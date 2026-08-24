"""M17 — experiment harness over cached member scores (Stage-3 variants).

Re-runs the SAME event book (the ONE engine, G-15) with one lever changed per
variant, on the SAME cached Stage-1/Stage-2 score parquets — so a variant's
delta vs baseline is attributable to that lever alone. No model is retrained.
Levers are the ones the blueprint leaves open or lists as tunable: ensemble
weights (M17/M10-02 [IMPL]), barrier m/h within spec ranges, a barrier width
cap ([IMPL], RUN_REPORT finding #1), and earnings-skip entries.

Every variant lands in the G-09 trials ledger: DSR's N grows with every
experiment run here, by design — that is what keeps the winner honest.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import load_config, git_sha
from src.pipeline.common import prepare
from src.ensemble.rank import member_ranks, deciles
from src.backtest.costs import CostModel
from src.backtest.engines.barriers_event import run_event_backtest
from src.features.events import event_features
from src.portfolio.construct import vol_target_scale
from src.primitives.fwd import forward_return
from src.regime.overlay import regime_multiplier
from src.validation.metrics import sharpe, max_drawdown
from src.hpo.determinism import seed_everything, artifact_stamp
from src.hpo.ledger import TrialsLedger

MEMBERS_ALL = ["lgbm_h5", "lgbm_h20", "lgbm_h60",
               "gru_h5", "gru_h20", "gru_h60", "cnn_I5R20"]

# IC horizon used for trailing member weights; weights at decision date t may
# only use ICs whose forward window closed at t (lag = 1 + horizon + 1).
IC_HORIZON = 20
IC_LAG = IC_HORIZON + 2
IC_WINDOW = 252


def load_scores(scores_dir: Path, members: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for m in members:
        p = Path(scores_dir) / f"scores_{m}.parquet"
        if p.exists():
            out[m] = pd.read_parquet(p)
    return out


def daily_rank_ic(rank: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """Per-day cross-sectional Spearman IC of an already-ranked frame vs fwd."""
    fr = fwd.rank(axis=1, pct=True)
    x, y = rank.align(fr, join="inner")
    xv, yv = x.to_numpy(float), y.to_numpy(float)
    ok = np.isfinite(xv) & np.isfinite(yv)
    xm = np.where(ok, xv, np.nan)
    ym = np.where(ok, yv, np.nan)
    n = ok.sum(axis=1)
    with np.errstate(invalid="ignore"):
        xc = xm - np.nanmean(xm, axis=1, keepdims=True)
        yc = ym - np.nanmean(ym, axis=1, keepdims=True)
        cov = np.nansum(xc * yc, axis=1)
        den = np.sqrt(np.nansum(xc ** 2, axis=1) * np.nansum(yc ** 2, axis=1))
        ic = np.where((n > 10) & (den > 0), cov / den, np.nan)
    return pd.Series(ic, index=x.index)


def trailing_ic_weights(ranks: dict[str, pd.DataFrame], fwd: pd.DataFrame,
                        window: int = IC_WINDOW, lag: int = IC_LAG) -> pd.DataFrame:
    """Causal per-day member weights: trailing mean daily RankIC over `window`
    sessions, lagged `lag` sessions so every IC's forward window has closed by
    the decision date. Negative trailing ICs floor at 0; all-zero days fall
    back to equal weights."""
    ics = pd.DataFrame({m: daily_rank_ic(r, fwd) for m, r in ranks.items()})
    w = ics.rolling(window, min_periods=window // 4).mean().shift(lag)
    w = w.clip(lower=0.0)
    tot = w.sum(axis=1)
    flat = ~(tot > 0)
    w = w.div(tot.where(tot > 0), axis=0)
    if flat.any():
        w.loc[flat] = 1.0 / len(ranks)
    return w


def weighted_ensemble(ranks: dict[str, pd.DataFrame], mask: pd.DataFrame,
                      day_w: pd.DataFrame | None = None) -> pd.DataFrame:
    """Mean of member ranks; optionally weighted per-day (columns = members).
    Weight mass renormalizes over the members present for each cell."""
    if day_w is None:
        day_w = pd.DataFrame(1.0, index=next(iter(ranks.values())).index,
                             columns=list(ranks))
    num = None
    den = None
    for m, r in ranks.items():
        wcol = day_w[m].reindex(r.index).fillna(0.0)
        contrib = r.mul(wcol, axis=0)
        wpres = r.notna().mul(wcol, axis=0)
        num = contrib if num is None else num.add(contrib, fill_value=0.0)
        den = wpres if den is None else den.add(wpres, fill_value=0.0)
    return (num / den.where(den > 0)).where(mask)


def _book_metrics(res: dict, dates_active: pd.DatetimeIndex) -> dict:
    dn = res["daily_net"]
    tr = res["trades"]
    cost_nav = float((tr["tranche_w"] * (tr["exit_ret_gross"]
                                         - tr["exit_ret_net"])).sum()) if len(tr) else 0.0
    yrs = max(1e-9, len(dates_active) / 252)
    return {"sharpe_net": sharpe(dn), "mdd": max_drawdown(dn),
            "ann_return": float(dn.mean() * 252), "ann_vol": float(dn.std() * np.sqrt(252)),
            "n_trades": int(res["n_trades"]), "avg_hold": res["avg_hold"],
            "hit_counts": res["hit_counts"],
            "cost_nav_per_yr": cost_nav / yrs,
            "win_rate": float((tr["exit_ret_net"] > 0).mean()) if len(tr) else float("nan")}


def default_variants() -> list[dict]:
    b = {"scores": "stage2", "weighting": "equal", "skip_earnings": False,
         "m": None, "h": None, "thr_cap": None}
    return [
        {**b, "name": "baseline"},
        {**b, "name": "icir_w", "weighting": "icir"},
        {**b, "name": "no_h5", "members": ["lgbm_h20", "lgbm_h60", "gru_h20", "gru_h60"]},
        {**b, "name": "earnskip", "skip_earnings": True},
        {**b, "name": "cap25", "thr_cap": 0.25},
        {**b, "name": "cap15", "thr_cap": 0.15},
        {**b, "name": "h40", "m": 1.5, "h": 40},
        {**b, "name": "m20_h40", "m": 2.0, "h": 40},
        {**b, "name": "m20_h60", "m": 2.0, "h": 60},
        {**b, "name": "lgbm_news", "members": ["lgbm_h5", "lgbm_h20", "lgbm_h60"]},
        {**b, "name": "lgbm_nonews", "scores": "stage1",
         "members": ["lgbm_h5", "lgbm_h20", "lgbm_h60"]},
        {**b, "name": "combo", "weighting": "icir", "skip_earnings": True,
         "m": 1.5, "h": 40, "thr_cap": 0.25},
    ]


def run_experiments(m1_dir: str, eod_dir: str, out_dir: str, scores_dir: str,
                    scores_dir_alt: str | None = None, config_path: str | None = None,
                    market_dir: str | None = None,
                    variants: list[dict] | None = None) -> dict:
    cfg, config_hash = load_config(config_path)
    seed = int(cfg.seed["global"])
    seed_everything(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    variants = variants or default_variants()

    d = prepare(cfg, m1_dir, eod_dir, market_dir, features=False)
    panel, mask, sigma32 = d["panel"], d["mask"], d["sigma32"]
    if d["health"]["blocking"]:
        raise RuntimeError(f"Q-004 BLOCKING failure: {d['health']}")
    cm = CostModel(per_trade_bps=float(cfg.cost.per_trade_bps),
                   borrow_gc_bps_yr=float(cfg.cost.borrow_gc_bps_yr),
                   borrow_table=d["m1"].borrow_fees()
                   if hasattr(d["m1"], "borrow_fees") else None)
    fwd = forward_return(panel.adj_close, IC_HORIZON).where(mask)

    # earnings_within_2d as raw booleans (the F8 feature pre-normalization)
    earn2 = None
    try:
        ev = event_features(d["m1"].earnings_surprises(), d["m1"].estimates_pit(),
                            panel.raw_close, panel.dates)
        earn2 = ev["earnings_within_2d"].fillna(0.0) > 0
    except Exception as e:  # noqa: BLE001 — earnings calendar is enrichment here
        print(f"!! earnings_within_2d unavailable ({type(e).__name__}: {e}) — "
              f"skip_earnings variants will not filter", flush=True)

    sets: dict[str, dict[str, pd.DataFrame]] = {}
    sets["stage2"] = load_scores(Path(scores_dir), MEMBERS_ALL)
    if scores_dir_alt:
        sets["stage1"] = load_scores(Path(scores_dir_alt),
                                     ["lgbm_h5", "lgbm_h20", "lgbm_h60"])
    if not sets["stage2"]:
        raise FileNotFoundError(f"no scores_*.parquet under {scores_dir}")
    print(f"score sets: " + ", ".join(f"{k}={list(v)}" for k, v in sets.items()),
          flush=True)

    ledger = TrialsLedger()
    results = []
    rank_cache: dict[tuple, dict[str, pd.DataFrame]] = {}
    for v in variants:
        name = v["name"]
        sset = sets.get(v.get("scores", "stage2"))
        if not sset:
            print(f"-- {name}: score set '{v.get('scores')}' missing, skipped", flush=True)
            continue
        members = [m for m in v.get("members", list(sset)) if m in sset]
        # M10-03 admission: cnn failed the floor in stage2 — exclude unless asked
        if "members" not in v:
            members = [m for m in members if m != "cnn_I5R20"]
        key = (v.get("scores", "stage2"), tuple(members))
        if key not in rank_cache:
            test_dates = pd.DatetimeIndex(sorted(set().union(
                *[set(sset[m].dropna(how="all").index) for m in members])))
            sub = {m: sset[m].reindex(test_dates) for m in members}
            rank_cache[key] = member_ranks(sub, mask.reindex(test_dates).fillna(False))
        ranks = rank_cache[key]
        test_dates = next(iter(ranks.values())).index
        msk = mask.reindex(test_dates).fillna(False)

        # regime / vol-target overrides ([IMPL] values; spec fixes the 0.5 mult)
        gm = regime_multiplier(
            d["idx_blk"],
            float(v.get("vol_thr") or cfg.regime.vol_threshold_ann),
            float(v.get("risk_off") or cfg.regime.gross_multiplier_risk_off))
        vt_target = float(v.get("vt") or cfg.port.vol_target_ann)
        vt_cap = float(v.get("vt_cap") or cfg.port.vol_target_scale_cap)

        day_w = None
        if v.get("weighting") == "icir":
            day_w = trailing_ic_weights(ranks, fwd.reindex(test_dates))
        ens = weighted_ensemble(ranks, msk, day_w)
        dec = deciles(ens, msk)
        sel = dec.eq(10)
        if v.get("skip_earnings") and earn2 is not None:
            sel = sel & ~earn2.reindex(test_dates).reindex(columns=sel.columns) \
                .fillna(False)

        gm_series = gm.reindex(test_dates).fillna(1.0)
        kw = dict(m=v.get("m"), h=v.get("h"), thr_cap=v.get("thr_cap"))
        pre = run_event_backtest(sel, panel, sigma32, cm, cfg,
                                 day_budget_mult=gm_series, **kw)
        vt = vol_target_scale(pre["daily_net"], vt_target, vt_cap)
        budget = (gm_series * vt.reindex(test_dates).fillna(1.0)).clip(lower=0.0)
        res = run_event_backtest(sel, panel, sigma32, cm, cfg,
                                 day_budget_mult=budget, **kw)

        met = _book_metrics(res, test_dates)
        met["ic_rank"] = float(daily_rank_ic(ens.rank(axis=1, pct=True),
                                             fwd.reindex(test_dates)).mean())
        row = {"name": name, **{k: v.get(k) for k in
                                ("scores", "weighting", "skip_earnings", "m", "h",
                                 "thr_cap", "vol_thr", "vt")},
               "members": members, **met}
        results.append(row)
        ledger.append(f"exp:{name}", {k: row[k] for k in
                                      ("scores", "weighting", "skip_earnings",
                                       "m", "h", "thr_cap", "vol_thr", "vt")},
                      config_hash, "stage3_variant", met["sharpe_net"],
                      note="experiments")
        res["daily_net"].to_frame("net").to_parquet(out / f"daily_net_{name}.parquet")
        print(f"== {name}: SR {met['sharpe_net']:.3f}  MDD {met['mdd']:.1%}  "
              f"ann {met['ann_return']:.1%}  IC {met['ic_rank']:.4f}  "
              f"trades {met['n_trades']:,}  cost/yr {met['cost_nav_per_yr']:.2%}",
              flush=True)

    report = {"stage": "experiments",
              "stamp": artifact_stamp(config_hash, d["m1"].data_snapshot_id(),
                                      git_sha(), seed),
              "n_trials_ledger": ledger.n_trials(),
              "results": sorted(results, key=lambda r: -r["sharpe_net"])}
    (out / "experiments_report.json").write_text(
        json.dumps(report, indent=2, default=str))
    print(json.dumps([{k: r[k] for k in ("name", "sharpe_net", "mdd", "ann_return",
                                         "ic_rank", "n_trades")}
                      for r in report["results"]], indent=2, default=str), flush=True)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default=os.environ.get("M1_DIR", "/workspace/m1"))
    ap.add_argument("--eod", default=os.environ.get("EOD_DIR", "/workspace/data"))
    ap.add_argument("--out", default=os.environ.get("OUT_DIR", "artifacts/reports/exp"))
    ap.add_argument("--scores", default=os.environ.get("SCORES_DIR",
                                                       "/workspace/derived/stage2"))
    ap.add_argument("--scores-alt", default=os.environ.get("SCORES_DIR_ALT",
                                                           "/workspace/derived/stage1"))
    ap.add_argument("--market", default=os.environ.get("MARKET_DIR") or None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--variants", default=os.environ.get("VARIANTS") or None,
                    help="JSON list of variant dicts; default = built-in grid")
    a = ap.parse_args()
    raw = a.variants
    if not raw and os.environ.get("VARIANTS_B64"):
        import base64
        raw = base64.b64decode(os.environ["VARIANTS_B64"]).decode()
    vs = json.loads(raw) if raw else None
    run_experiments(a.m1, a.eod, a.out, a.scores, a.scores_alt, a.config, a.market,
                    variants=vs)
