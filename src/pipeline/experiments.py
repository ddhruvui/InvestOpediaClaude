"""M17 — experiment harness over cached member scores (Stage-3 variants).

Re-runs the SAME event book (the ONE engine, G-15) with one lever changed per
variant, on the SAME cached Stage-1/Stage-2 score parquets — so a variant's
delta vs baseline is attributable to that lever alone. No model is retrained.
Levers are the ones the blueprint leaves open or lists as tunable: ensemble
weights (M17/M10-02 [IMPL]), barrier m/h within spec ranges, a barrier width
cap ([IMPL], RUN_REPORT finding #1), and earnings-skip entries.

Every variant lands in the G-09 trials ledger: DSR's N grows with every
experiment run here, by design — that is what keeps the winner honest.

Aggressive-book levers (branch aggressive-short-horizon): top_n concentration,
tranche count (capital utilization at short holds), single-name cap, asymmetric
barriers (m_up/m_dn), per-variant cost bps, and a FinBERT sentiment entry gate.
Per-variant metrics now carry CAGR, calendar-year returns, subperiod blocks and
realized gross exposure so aggressive variants are compared honestly.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import Cfg, load_config, git_sha
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


def _patch_cfg(cfg, tranches: int | None = None, name_cap: float | None = None):
    """Read-only Cfg with per-variant port overrides (engine reads cfg.port.*)."""
    if tranches is None and name_cap is None:
        return cfg
    d = cfg.to_dict()
    port = dict(d["port"])
    if tranches is not None:
        port["tranches"] = int(tranches)
    if name_cap is not None:
        port["single_name_cap"] = float(name_cap)
    d["port"] = port
    return Cfg(d)


# 16:00 ET ~= 21:00 UTC (same convention as src/features/sentiment.py, which we
# do not import here — its scoring path drags in transformers on a CPU pod).
CLOSE_UTC_HOUR = 21


def sentiment_ewm3(cache_path: str | Path, dates: pd.DatetimeIndex,
                   tickers: pd.Index) -> pd.DataFrame | None:
    """Per-name 3-session EWM of FinBERT headline scores from the stage2 cache
    (columns ticker/ts/title/s). After-close headlines belong to the NEXT
    session — same cutoff as F9. Names/dates without coverage stay NaN."""
    p = Path(cache_path)
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty or "s" not in df:
        return None
    eff = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(None)
    after_close = eff.dt.hour >= CLOSE_UTC_HOUR
    eff_date = eff.dt.normalize() + pd.to_timedelta(after_close.astype(int), unit="D")
    pos = np.searchsorted(dates.values, eff_date.values, side="left")
    pos = np.clip(pos, 0, len(dates) - 1)
    df = df.assign(session=dates.values[pos])
    mean = (df.groupby(["session", "ticker"], observed=True)["s"].mean()
              .unstack("ticker").reindex(index=dates, columns=tickers))
    return mean.ewm(span=3, adjust=False, min_periods=1).mean() \
               .where(mean.notna().rolling(10, min_periods=1).max() > 0)


def _gross_exposure(trades: pd.DataFrame, dates: pd.DatetimeIndex) -> dict:
    """Realized gross invested (sum of live tranche weights) — the honesty
    metric for utilization/leverage comparisons across variants."""
    if not len(trades):
        return {"avg_gross": 0.0, "max_gross": 0.0}
    g_in = trades.groupby("fill_date")["tranche_w"].sum()
    g_out = trades.groupby("exit_date")["tranche_w"].sum()
    flow = g_in.reindex(dates, fill_value=0.0) - g_out.reindex(dates, fill_value=0.0)
    gross = flow.cumsum()
    return {"avg_gross": float(gross.mean()), "max_gross": float(gross.max())}


def _period_block(dn: pd.Series) -> dict | None:
    dn = dn.dropna()
    if len(dn) < 60:
        return None
    eq = (1 + dn).cumprod()
    yrs = len(dn) / 252
    return {"sharpe": sharpe(dn), "ann_return": float(dn.mean() * 252),
            "cagr": float(eq.iloc[-1] ** (1 / yrs) - 1),
            "ann_vol": float(dn.std() * np.sqrt(252)),
            "mdd": max_drawdown(dn), "n_days": int(len(dn))}


def _sub_metrics(dn: pd.Series) -> dict:
    """Subperiod blocks + calendar-year net returns. 'news_era' (2021+) is the
    only window where the FinBERT overlay has coverage; 'last5y'/'last3y' answer
    'what would this book have done recently', which a 19y average hides."""
    end = dn.index.max()
    out = {"full": _period_block(dn),
           "last5y": _period_block(dn[dn.index >= end - pd.DateOffset(years=5)]),
           "last3y": _period_block(dn[dn.index >= end - pd.DateOffset(years=3)]),
           "news_era": _period_block(dn[dn.index >= "2021-01-01"])}
    out["by_year"] = {str(y): float((1 + g).prod() - 1)
                      for y, g in dn.groupby(dn.index.year)}
    return out


def _book_metrics(res: dict, dates_active: pd.DatetimeIndex) -> dict:
    # metrics on the scored window only — the engine's series spans the whole
    # panel (2000+) but scores start later; leading flat years dilute CAGR
    dn = res["daily_net"].reindex(dates_active).dropna()
    tr = res["trades"]
    if res.get("cost_daily") is not None:
        cost_nav = float(res["cost_daily"].sum())   # actual charged (nets MOO legs)
    else:
        cost_nav = float((tr["tranche_w"] * (tr["exit_ret_gross"]
                                             - tr["exit_ret_net"])).sum()) if len(tr) else 0.0
    yrs = max(1e-9, len(dates_active) / 252)
    eq = (1 + dn.fillna(0.0)).cumprod()
    cagr = float(eq.iloc[-1] ** (252 / max(1, len(dn))) - 1) if len(dn) else float("nan")
    return {"sharpe_net": sharpe(dn), "mdd": max_drawdown(dn),
            "ann_return": float(dn.mean() * 252), "cagr": cagr,
            "ann_vol": float(dn.std() * np.sqrt(252)),
            "n_trades": int(res["n_trades"]), "avg_hold": res["avg_hold"],
            "hit_counts": res["hit_counts"],
            "cost_nav_per_yr": cost_nav / yrs,
            "win_rate": float((tr["exit_ret_net"] > 0).mean()) if len(tr) else float("nan"),
            **_gross_exposure(tr, dates_active),
            "periods": _sub_metrics(dn)}


def default_variants() -> list[dict]:
    """Aggressive short-horizon exploration grid. `baseline` = the adopted h60
    book (anchor + parity check); everything else trades holding time for
    concentration and utilization. Pass VARIANTS/VARIANTS_B64 to override."""
    b = {"scores": "stage2", "weighting": "equal", "skip_earnings": False,
         "m": None, "h": None, "thr_cap": None}
    top20 = {**b, "h": 5, "tranches": 5, "top_n": 20, "name_cap": 0.10}
    return [
        {**b, "name": "baseline"},
        {**b, "name": "h5_matched", "h": 5, "tranches": 5},
        {**top20, "name": "h5_top20"},
        {**top20, "name": "h3_top20", "h": 3, "tranches": 3},
        {**top20, "name": "h7_top20", "h": 7, "tranches": 7},
        {**top20, "name": "h10_top20", "h": 10, "tranches": 10},
        {**top20, "name": "h5_top10", "top_n": 10, "name_cap": 0.15},
        {**top20, "name": "h5_top50", "top_n": 50, "name_cap": 0.06},
        {**top20, "name": "h5_top20_vt25", "vt": 0.25, "vt_cap": 2.5},
        {**top20, "name": "h5_top20_m075", "m": 0.75},
        {**top20, "name": "h5_top20_asym", "m_up": 1.0, "m_dn": 2.0},
        {**top20, "name": "h5_top20_earnskip", "skip_earnings": True},
        {**top20, "name": "h5_top20_sent", "sent_gate": -0.10},
        {**top20, "name": "h5_top20_fast",
         "members": ["lgbm_h5", "gru_h5", "lgbm_h20", "gru_h20"]},
        {**top20, "name": "h5_top20_cost30", "cost_bps": 30},
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

    # FinBERT overlay: load once if any variant gates on sentiment
    sent = None
    if any(v.get("sent_gate") is not None for v in variants):
        sent = sentiment_ewm3(Path(scores_dir) / "sentiment_scores.parquet",
                              panel.dates, panel.tickers)
        cov = float(sent.notna().any(axis=0).mean()) if sent is not None else 0.0
        print(f"sentiment overlay: {'loaded' if sent is not None else 'MISSING'} "
              f"(name coverage {cov:.0%})", flush=True)

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
        if v.get("top_n"):
            # concentration lever: absolute top-N per day (deciles are 10% of a
            # 1000-name universe — far too diffuse for an aggressive book)
            sel = ens.rank(axis=1, ascending=False, method="first") \
                     .le(int(v["top_n"])) & msk
        else:
            dec = deciles(ens, msk)
            sel = dec.eq(10)
        if v.get("skip_earnings") and earn2 is not None:
            sel = sel & ~earn2.reindex(test_dates).reindex(columns=sel.columns) \
                .fillna(False)
        if v.get("sent_gate") is not None and sent is not None:
            # skip entries only where coverage EXISTS and is below the gate —
            # uncovered names pass (coverage is 2020-12+, ~500 names)
            bad = sent.reindex(test_dates).reindex(columns=sel.columns) \
                      .le(float(v["sent_gate"])).fillna(False)
            sel = sel & ~bad

        cfg_v = _patch_cfg(cfg, v.get("tranches"), v.get("name_cap"))
        cm_v = cm
        if v.get("cost_bps") is not None:
            cm_v = CostModel(per_trade_bps=float(v["cost_bps"]),
                             borrow_gc_bps_yr=float(cfg.cost.borrow_gc_bps_yr),
                             borrow_table=d["m1"].borrow_fees()
                             if hasattr(d["m1"], "borrow_fees") else None)

        gm_series = gm.reindex(test_dates).fillna(1.0)
        kw = dict(m=v.get("m"), h=v.get("h"), thr_cap=v.get("thr_cap"),
                  m_up=v.get("m_up"), m_dn=v.get("m_dn"),
                  net_moo_costs=bool(v.get("net_moo")))
        pre = run_event_backtest(sel, panel, sigma32, cm_v, cfg_v,
                                 day_budget_mult=gm_series, **kw)
        vt = vol_target_scale(pre["daily_net"], vt_target, vt_cap)
        budget = (gm_series * vt.reindex(test_dates).fillna(1.0)).clip(lower=0.0)
        res = run_event_backtest(sel, panel, sigma32, cm_v, cfg_v,
                                 day_budget_mult=budget, **kw)

        if v.get("fin_bps_yr"):
            # margin financing on gross above 1x NAV (the engine models no
            # cash constraint; a vol-targeted concentrated book runs >1x)
            tr_res = res["trades"]
            idx = res["daily_net"].index
            g_in = tr_res.groupby("fill_date")["tranche_w"].sum()
            g_out = tr_res.groupby("exit_date")["tranche_w"].sum()
            gross_d = (g_in.reindex(idx, fill_value=0.0)
                       - g_out.reindex(idx, fill_value=0.0)).cumsum()
            drag = (gross_d - 1.0).clip(lower=0.0) \
                * float(v["fin_bps_yr"]) / 1e4 / 252.0
            res["daily_net"] = res["daily_net"] - drag

        met = _book_metrics(res, test_dates)
        met["ic_rank"] = float(daily_rank_ic(ens.rank(axis=1, pct=True),
                                             fwd.reindex(test_dates)).mean())
        LEVERS = ("scores", "weighting", "skip_earnings", "m", "h", "thr_cap",
                  "vol_thr", "vt", "vt_cap", "top_n", "tranches", "name_cap",
                  "m_up", "m_dn", "cost_bps", "sent_gate", "net_moo",
                  "fin_bps_yr")
        row = {"name": name, **{k: v.get(k) for k in LEVERS},
               "members": members, **met}
        results.append(row)
        ledger.append(f"exp:{name}", {k: row[k] for k in LEVERS},
                      config_hash, "stage3_variant", met["sharpe_net"],
                      note="experiments")
        res["daily_net"].to_frame("net").to_parquet(out / f"daily_net_{name}.parquet")
        print(f"== {name}: SR {met['sharpe_net']:.3f}  MDD {met['mdd']:.1%}  "
              f"CAGR {met['cagr']:.1%}  ann {met['ann_return']:.1%}  "
              f"IC {met['ic_rank']:.4f}  trades {met['n_trades']:,}  "
              f"hold {met['avg_hold']:.1f}d  gross {met['avg_gross']:.2f}x  "
              f"cost/yr {met['cost_nav_per_yr']:.2%}", flush=True)

    report = {"stage": "experiments",
              "stamp": artifact_stamp(config_hash, d["m1"].data_snapshot_id(),
                                      git_sha(), seed),
              "n_trials_ledger": ledger.n_trials(),
              "results": sorted(results, key=lambda r: -r["sharpe_net"])}
    (out / "experiments_report.json").write_text(
        json.dumps(report, indent=2, default=str))
    print(json.dumps([{k: r[k] for k in ("name", "sharpe_net", "mdd", "ann_return",
                                         "cagr", "avg_hold", "avg_gross",
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
