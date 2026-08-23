#!/usr/bin/env python3
"""Assemble pod artifacts into the reports/ bundle the app serves.

Reads whatever stage1/2/3 + predict artifacts are present (downloaded from the
RunPod volume) and writes small, self-describing JSON the backend can serve
without any compute:

    reports/latest/summary.json        gates, performance, baselines, members, CPCV, DSR
    reports/latest/equity.json         book equity curve (+ drawdown), downsampled
    reports/latest/suggestions.json    current target book with barrier levels
    reports/latest/trades_summary.json suggested->outcome aggregates
    reports/latest/trades_sample.json  most recent N trades for the explorer table
    reports/latest/manifest.json       provenance: which artifact fed which section

The heavy ledger (hundreds of thousands of barrier trades) never reaches the
browser: aggregates + a recent slice do.

    python3 tools/build_reports.py --src derived --out reports/latest
"""
from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

MAX_EQUITY_POINTS = 1500      # ~19y of daily -> weekly-ish; keeps the payload small
TRADE_SAMPLE = 4000           # most recent trades exposed to the table


def _read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def _read_parquet(p: Path):
    try:
        return pd.read_parquet(p)
    except Exception:
        return None


def _clean(o):
    """JSON-safe: NaN/Inf -> None, numpy scalars -> python."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 8)
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.strftime("%Y-%m-%d")
    return o


# ----------------------------------------------------------------- summary
def build_summary(src: Path) -> tuple[dict, dict]:
    prov = {}
    s1 = _read_json(src / "stage1_report_clean.json") or _read_json(src / "stage1_report.json")
    s2 = _read_json(src / "stage2_report.json")
    s3 = _read_json(src / "stage3_report.json") or _read_json(src / "stage3_final_report.json")
    s3_cpcv = _read_json(src / "stage3_final_report.json")

    # The ensemble stage (stage2) owns member/IC/baseline truth; stage3 owns the
    # deployed book (barrier exits + overlays) and the CPCV distribution.
    base = s2 or s1 or {}
    prov["members"] = "stage2_report.json" if s2 else "stage1_report_clean.json"

    members = [{"name": k, **{kk: v[kk] for kk in ("IC", "RankIC", "ICIR", "RankICIR")}}
               for k, v in (base.get("members") or {}).items()]
    members.sort(key=lambda m: (m["RankIC"] is None, -(m["RankIC"] or 0)))

    book = {}
    if s3:
        ad = s3.get("adoption") or {}
        ung, gat = ad.get("ungated") or {}, ad.get("gated") or {}
        book = {
            "engine": "event (M5.2 barrier exits, regime + vol targeting)",
            "sharpe_net": ung.get("sharpe"),
            "mdd": ung.get("mdd"),
            "n_trades": ung.get("n_trades"),
            "avg_hold_sessions": ung.get("avg_hold"),
            "exit_mix": ung.get("hit_counts") or {},
            "ann_return": (s3.get("book") or {}).get("ann_return"),
            "ann_vol": (s3.get("book") or {}).get("ann_vol"),
            "meta_gate": {
                "adopted": ad.get("adopt"),
                "sharpe": gat.get("sharpe"),
                "n_trades": gat.get("n_trades"),
                "mdd": gat.get("mdd"),
                "turnover_cut": ad.get("turnover_cut"),
                "rule": "M11-02: ships only if turnover falls materially at "
                        "equal-or-better net Sharpe",
            },
            "dsr": {**(s3.get("dsr") or {}),
                "caveat": "N counts the trials ledger visible to the run that "
                          "produced this book. A ledger that does not persist "
                          "across runs undercounts N and under-deflates the "
                          "Sharpe (G-09)."},
        }
        prov["book"] = "stage3_report.json"

    fast = (base.get("performance_15bps") or {})
    baselines = base.get("baselines") or {}
    ic = base.get("ic_ensemble_vs_fwd20") or {}

    # G-11 gates evaluated against the DEPLOYED (event-engine) book
    sharpe = book.get("sharpe_net")
    mdd = book.get("mdd")
    rank_ic = ic.get("RankIC")
    spy_sharpe = ((baselines.get("spy_bh") or {}).get("sharpe_net"))
    mom_sharpe = ((baselines.get("mom_12_1") or {}).get("sharpe_net"))
    beats_spy = bool(sharpe is not None and spy_sharpe is not None and sharpe > spy_sharpe)
    beats_mom = bool(sharpe is not None and mom_sharpe is not None and sharpe > mom_sharpe)
    kill = not (rank_ic and rank_ic >= 0.02) or not (sharpe and sharpe >= 0.5)
    advance = bool(sharpe and sharpe >= 0.8 and mdd and mdd > -0.15
                   and beats_spy and beats_mom)
    ceiling = bool(sharpe and (sharpe > 2.0 or (mdd is not None and mdd > -0.05)))
    verdict = ("LEAKAGE-AUDIT" if ceiling else "KILL" if kill
               else "ADVANCE" if advance else "ITERATE")

    gates = {
        "verdict": verdict,
        "checks": [
            {"id": "G-11 kill: Rank IC", "value": rank_ic, "threshold": 0.02,
             "op": ">=", "pass": bool(rank_ic and rank_ic >= 0.02),
             "note": "net-of-cost walk-forward Rank IC"},
            {"id": "G-11 kill: net Sharpe", "value": sharpe, "threshold": 0.5,
             "op": ">=", "pass": bool(sharpe and sharpe >= 0.5),
             "note": "below this, do not trade"},
            {"id": "G-11 advance: net Sharpe", "value": sharpe, "threshold": 0.8,
             "op": ">=", "pass": bool(sharpe and sharpe >= 0.8),
             "note": "paper-trading bar"},
            {"id": "G-11 advance: max drawdown", "value": mdd, "threshold": -0.15,
             "op": ">", "pass": bool(mdd is not None and mdd > -0.15),
             "note": "across CPCV paths"},
            {"id": "G-08 beat SPY buy-and-hold", "value": sharpe,
             "threshold": spy_sharpe, "op": ">", "pass": beats_spy,
             "note": "free baseline"},
            {"id": "G-08 beat 12-1 momentum", "value": sharpe,
             "threshold": mom_sharpe, "op": ">", "pass": beats_mom,
             "note": "free baseline"},
        ],
    }

    cpcv = (s3_cpcv or {}).get("cpcv") or (s3 or {}).get("cpcv")
    if cpcv:
        prov["cpcv"] = "stage3_final_report.json"

    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stamp": (s3 or base).get("stamp", {}),
        "universe": base.get("universe", {}),
        "gates": gates,
        "book": book,
        "fast_path": {"sharpe_net": fast.get("sharpe_net"), "mdd": fast.get("mdd"),
                      "turnover_month": fast.get("turnover_month"),
                      "note": "Stage-1 fixed 15-tranche rotation (no barrier exits)"},
        "ic": ic,
        "members": members,
        "member_admission_floor": 0.02,
        "baselines": {
            "spy_bh": baselines.get("spy_bh"),
            "mom_12_1": baselines.get("mom_12_1"),
            "alpha_beta_vs_spy": baselines.get("alpha_beta_vs_spy"),
            "alpha_beta_vs_mom": baselines.get("alpha_beta_vs_mom"),
        },
        "sensitivity": base.get("sensitivity", {}),
        "cpcv": cpcv,
        "ensemble_vs_best_single": base.get("ensemble_vs_best_single"),
        "caveat": "Research output evaluated under blueprint v1.0.1 gates. "
                  "Not financial advice.",
    }
    return _clean(summary), prov


# ----------------------------------------------------------------- equity
def build_equity(src: Path) -> dict | None:
    eq = _read_parquet(src / "stage3_equity.parquet")
    col = "equity"
    if eq is None:
        eq = _read_parquet(src / "stage3_daily_net.parquet")
        if eq is None:
            return None
        eq = (1 + eq["net"].fillna(0)).cumprod().to_frame("equity")
    s = eq[col] if col in eq else eq.iloc[:, 0]
    s = s.dropna()
    if s.empty:
        return None
    # The event engine spans the whole panel, but the book only exists from the
    # first walk-forward test date — trim the flat lead so the curve starts where
    # capital was actually at risk.
    live = s[(s - s.iloc[0]).abs() > 1e-9]
    if len(live):
        s = s.loc[live.index[0]:]
    dd = s / s.cummax() - 1
    step = max(1, len(s) // MAX_EQUITY_POINTS)
    idx = list(range(0, len(s), step))
    if idx[-1] != len(s) - 1:
        idx.append(len(s) - 1)
    return _clean({
        "series": [{"date": s.index[i].strftime("%Y-%m-%d"),
                    "equity": float(s.iloc[i]), "drawdown": float(dd.iloc[i])}
                   for i in idx],
        "start": s.index[0].strftime("%Y-%m-%d"),
        "end": s.index[-1].strftime("%Y-%m-%d"),
        "final_equity": float(s.iloc[-1]),
        "max_drawdown": float(dd.min()),
    })


# ------------------------------------------------------ suggested -> outcome
def build_trades(src: Path) -> tuple[dict | None, dict | None]:
    tr = _read_parquet(src / "stage3_trades_ungated.parquet")
    if tr is None or not len(tr):
        return None, None
    tr = tr.copy()
    tr["entry_date"] = pd.to_datetime(tr["entry_date"])
    tr["exit_date"] = pd.to_datetime(tr["exit_date"])
    tr["year"] = tr["entry_date"].dt.year
    tr = tr[tr["exit_ret_net"].notna()]
    won = tr["exit_ret_net"] > 0

    def _blk(g: pd.DataFrame) -> dict:
        return {"n": int(len(g)),
                "win_rate": float((g["exit_ret_net"] > 0).mean()) if len(g) else None,
                "avg_ret": float(g["exit_ret_net"].mean()) if len(g) else None,
                "median_ret": float(g["exit_ret_net"].median()) if len(g) else None,
                "avg_hold": float(g["holding_days"].mean()) if len(g) else None}

    by_year = [{"year": int(y), **_blk(g)} for y, g in tr.groupby("year")]
    by_exit = [{"exit": k, **_blk(g)} for k, g in tr.groupby("barrier_hit")]

    # Conviction: does a stronger ensemble rank actually pay?
    by_conv = []
    if "ensemble_rank" in tr and tr["ensemble_rank"].notna().any():
        v = tr.dropna(subset=["ensemble_rank"]).copy()
        v["decile"] = pd.qcut(v["ensemble_rank"].rank(method="first"), 10,
                              labels=False, duplicates="drop") + 1
        by_conv = [{"decile": int(d), **_blk(g)} for d, g in v.groupby("decile")]

    # Return distribution (clipped tails so one outlier can't own the axis)
    r = tr["exit_ret_net"].clip(-0.5, 0.5)
    counts, edges = np.histogram(r, bins=41, range=(-0.5, 0.5))
    dist = [{"lo": float(edges[i]), "hi": float(edges[i + 1]), "n": int(counts[i])}
            for i in range(len(counts))]

    hold_bins = [(1, 1), (2, 3), (4, 5), (6, 10), (11, 15), (16, 20), (21, 100)]
    by_hold = []
    for lo, hi in hold_bins:
        g = tr[(tr["holding_days"] >= lo) & (tr["holding_days"] <= hi)]
        if len(g):
            by_hold.append({"bucket": f"{lo}" if lo == hi else f"{lo}-{hi}", **_blk(g)})

    summary = {
        "n_trades": int(len(tr)),
        "win_rate": float(won.mean()),
        "avg_ret": float(tr["exit_ret_net"].mean()),
        "median_ret": float(tr["exit_ret_net"].median()),
        "avg_hold": float(tr["holding_days"].mean()),
        "total_pt": int((tr["barrier_hit"] == "upper").sum()),
        "total_stop": int((tr["barrier_hit"] == "lower").sum()),
        "total_time": int((tr["barrier_hit"] == "vertical").sum()),
        "day_trades": int(tr["day_trade"].sum()) if "day_trade" in tr else 0,
        "date_range": [tr["entry_date"].min().strftime("%Y-%m-%d"),
                       tr["entry_date"].max().strftime("%Y-%m-%d")],
        "by_year": sorted(by_year, key=lambda x: x["year"]),
        "by_exit": by_exit,
        "by_conviction_decile": by_conv,
        "by_holding_bucket": by_hold,
        "return_distribution": dist,
    }

    cols = ["entry_date", "ticker", "side", "entry_price", "exit_date", "exit_price",
            "barrier_hit", "exit_ret_net", "exit_ret_gross", "holding_days",
            "day_trade", "ensemble_rank"]
    cols = [c for c in cols if c in tr.columns]
    sample = tr.sort_values("entry_date").tail(TRADE_SAMPLE)[cols].copy()
    sample["entry_date"] = sample["entry_date"].dt.strftime("%Y-%m-%d")
    sample["exit_date"] = sample["exit_date"].dt.strftime("%Y-%m-%d")
    return _clean(summary), _clean({"rows": sample.to_dict("records"),
                                    "n_total": int(len(tr)),
                                    "note": f"most recent {len(sample):,} of "
                                            f"{len(tr):,} barrier trades"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="derived", help="dir with pod artifacts")
    ap.add_argument("--out", default="reports/latest")
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "source_dir": str(src), "sections": {}}

    summary, prov = build_summary(src)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    manifest["sections"]["summary"] = prov
    print(f"OK summary.json      verdict={summary['gates']['verdict']} "
          f"members={len(summary['members'])}")

    eq = build_equity(src)
    if eq:
        (out / "equity.json").write_text(json.dumps(eq, indent=1))
        manifest["sections"]["equity"] = {"points": len(eq["series"])}
        print(f"OK equity.json       {len(eq['series'])} points "
              f"{eq['start']}..{eq['end']}")

    sug = _read_json(src / "suggestions.json")
    if sug:
        (out / "suggestions.json").write_text(json.dumps(_clean(sug), indent=1))
        manifest["sections"]["suggestions"] = {
            "as_of": sug.get("as_of_close"),
            "n": len(sug.get("buys_or_increases", []))}
        print(f"OK suggestions.json  as_of={sug.get('as_of_close')} "
              f"n={len(sug.get('buys_or_increases', []))}")

    # config the app must not re-invent (C-08 costs, barriers, compliance limits)
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.config import load_config
        cfg, config_hash = load_config()
        cfgj = {
            "config_hash": config_hash,
            "cost": {"per_trade_bps": cfg.cost.per_trade_bps,
                     "borrow_gc_bps_yr": cfg.cost.borrow_gc_bps_yr,
                     "slippage_bps": cfg.cost.slippage_bps},
            "barrier": {"m": cfg.barrier.m, "h_days": cfg.barrier.h_days,
                        "tie_break": cfg.barrier.tie_break},
            "port": {"single_name_cap": cfg.port.single_name_cap,
                     "vol_target_ann": cfg.port.vol_target_ann,
                     "tranches": cfg.port.tranches},
            "pdt": {"limit": 3, "window_business_days": 5, "equity_floor": 25000},
            "ops": {"max_daily_loss_pct": cfg.ops.max_daily_loss_pct},
            "slippage_adoption_min_fills": 60,
            "decay": {"window_sessions": 63, "breach_sessions": 126,
                      "ratio_of_backtest": 0.5},
        }
        (out / "config.json").write_text(json.dumps(_clean(cfgj), indent=1))
        manifest["sections"]["config"] = {"config_hash": config_hash}
        print(f"OK config.json       hash={config_hash[:12]}")
    except Exception as e:
        print(f"-- config: {type(e).__name__}: {e}")

    # D-11 session grid — the app needs it to answer "which session do these
    # orders belong to?" without re-deriving holidays.
    ses = _read_parquet(src / "sessions.parquet")
    if ses is not None and len(ses):
        dts = pd.to_datetime(ses["date"]).sort_values()
        today = pd.Timestamp(datetime.now(timezone.utc).date())
        window = dts[(dts >= today - pd.Timedelta(days=30))
                     & (dts <= today + pd.Timedelta(days=90))]
        (out / "calendar.json").write_text(json.dumps(_clean({
            "sessions": [d.strftime("%Y-%m-%d") for d in window],
            "note": "NYSE sessions (D-11 / Q-001) around today; holidays excluded.",
        }), indent=1))
        manifest["sections"]["calendar"] = {"sessions": len(window)}
        print(f"OK calendar.json     {len(window)} sessions around today")

    tsum, tsample = build_trades(src)
    if tsum:
        (out / "trades_summary.json").write_text(json.dumps(tsum, indent=1))
        (out / "trades_sample.json").write_text(json.dumps(tsample, indent=1))
        manifest["sections"]["trades"] = {"n_total": tsum["n_trades"],
                                          "sampled": len(tsample["rows"])}
        print(f"OK trades_*.json     {tsum['n_trades']:,} trades, "
              f"win rate {tsum['win_rate']:.1%}, sample {len(tsample['rows']):,}")
    else:
        print("-- trades: no stage3_trades_ungated.parquet in src (run stage3)")

    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"bundle -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
