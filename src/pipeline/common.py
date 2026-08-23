"""Shared data preparation for stage1/stage2/predict — one implementation (G-15)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.m1 import M1
from src.data.panel import build_panel
from src.data.universe import top_dollar_volume_mask
from src.data.health import health_report
from src.data.index_prices import load_spy
from src.features.pipeline import build_features
from src.labels.heads import horizon_labels
from src.primitives.returns import daily_return, log_return
from src.primitives.ewma import ewma_sigma
from src.primitives.index import index_block


def prepare(cfg, m1_dir: str, eod_dir: str, market_dir: str | None = None,
            max_tickers: int | None = None, with_sentiment: bool = False,
            finbert_dir: str | None = None, sent_cache: str | None = None) -> dict:
    m1 = M1(m1_dir)
    sessions = m1.sessions()
    if market_dir:
        from src.data.market import load_workset_prices, membership_mask
        prices = load_workset_prices(market_dir, m1.raw_prices())
    else:
        prices = m1.raw_prices()
    if max_tickers:
        keep = sorted(prices["ticker"].unique())[:max_tickers]
        prices = prices[prices["ticker"].isin(keep)]
    panel = build_panel(prices, m1.adjustment_factors(), m1.corporate_actions(), sessions)
    del prices                      # 16.8M-row long frame: GBs freed before features
    import gc
    gc.collect()
    print(f"panel: {panel.adj_close.shape[0]} sessions x {panel.adj_close.shape[1]} names "
          f"| {panel.meta}", flush=True)

    if market_dir:
        from src.data.market import membership_mask, fund_tickers
        ents = m1.entities()
        known = set(ents["ticker"].astype(str)) |             {t.replace(".", "-") for t in ents["ticker"].astype(str)}
        aliens = {t for t in map(str, panel.tickers) if t not in known}
        print(f"universe discipline: {len(aliens)} names lack a Sharadar entity "
              f"identity -> excluded (D-13/G-05)", flush=True)
        mask = membership_mask(market_dir, panel.dates, panel.tickers,
                               exclude=fund_tickers(ents) | aliens)
        mask &= panel.raw_close.notna()
    else:
        mask = top_dollar_volume_mask(
            panel.raw_close, panel.raw_volume,
            size=int(cfg.universe.size), window=int(cfg.universe.liquidity_window_days),
            hysteresis=float(cfg.universe.hysteresis),
            min_price=float(cfg.universe.min_price))

    health = health_report(panel, mask, expected_universe_band=(
        min(100, panel.adj_close.shape[1] // 2), int(cfg.universe.size) * 11 // 10))

    r = daily_return(panel.adj_close)
    sigma32 = ewma_sigma(log_return(r), span=int(cfg.barrier.sigma_span))
    spy = load_spy(eod_dir).reindex(panel.dates)
    idx_blk = index_block(spy["adj_close"], r, beta_window=int(cfg.port.hedge_beta_window))

    feats, manifest = build_features(
        panel, mask, fundamentals=m1.fundamentals_pit(), surprises=m1.earnings_surprises(),
        estimates=m1.estimates_pit(), sessions=sessions)
    if with_sentiment and finbert_dir:
        try:
            from src.features.sentiment import sentiment_features
            from src.primitives.csnorm import cs_transform
            import os
            news_dir = os.path.join(eod_dir, "news")
            sent = sentiment_features(news_dir, finbert_dir, panel.dates, panel.tickers,
                                      cache_path=sent_cache)
            for name, wide in sent.items():
                s = cs_transform(wide, mask, mode="rank").stack(future_stack=True)
                feats[name] = s.reindex(feats.index)
                manifest[name] = {"block": "F9", "formula_hash": "finbert_pinned"}
        except Exception as e:   # F9 is enrichment — the run survives without it
            print(f"!! F9 sentiment unavailable ({type(e).__name__}: {e}) — "
                  f"continuing without it", flush=True)
    feats = feats.astype(np.float32)
    labels = horizon_labels(panel.adj_close, mask, horizons=tuple(cfg.labels.horizons),
                            cs_norm=cfg.labels.cs_norm)
    ys = {}
    for n, w in labels.items():
        s = w.stack(future_stack=True)
        s.index.names = ["date", "ticker"]
        ys[n] = s
    print(f"features: {feats.shape}", flush=True)
    return {"m1": m1, "sessions": sessions, "panel": panel, "mask": mask,
            "health": health, "r": r, "sigma32": sigma32, "spy": spy,
            "idx_blk": idx_blk, "feats": feats, "manifest": manifest,
            "labels": labels, "ys": ys}


def evaluate_book(cfg, d: dict, scores: dict, folds, out, config_hash: str, seed: int,
                  ledger=None, fold_stats=None, regime_on: bool = False,
                  stage: str = "stage1", member_gate: bool = False) -> dict:
    """M10 ensemble -> M13 -> M14 targets -> M15 backtests -> M16 report.
    One implementation shared by every stage (G-15). member_gate applies M10-03:
    a member joins only if its own stitched RankIC clears the G-11 minimum."""
    import json
    import numpy as np
    import pandas as pd
    from pathlib import Path

    from src.config import git_sha
    from src.ensemble.rank import ensemble_rank, deciles
    from src.portfolio.construct import construct_targets, vol_target_scale, HEDGE_COL
    from src.backtest.costs import CostModel
    from src.backtest.engine import run_backtest
    from src.regime.overlay import regime_multiplier
    from src.validation.metrics import ic_summary, perf_summary, alpha_beta, sharpe
    from src.validation.baselines import spy_buy_hold, plain_momentum
    from src.validation.gates import evaluate_gates
    from src.validation.dsr import deflated_sharpe
    from src.hpo.determinism import artifact_stamp
    from src.hpo.ledger import TrialsLedger
    from src.primitives.fwd import forward_return

    out = Path(out)
    panel, mask, sigma32, spy, idx_blk, m1 = (d["panel"], d["mask"], d["sigma32"],
                                              d["spy"], d["idx_blk"], d["m1"])
    ledger = ledger or TrialsLedger()
    dates = panel.dates
    test_dates = pd.DatetimeIndex(sorted(set().union(*[set(f.test) for f in folds])))
    fwd20 = forward_return(panel.adj_close, 20).loc[test_dates]

    # ---- M10-03 member admission + ensemble ----
    member_ics = {}
    admitted = {}
    for name, sc in scores.items():
        mi = ic_summary(sc.loc[test_dates], fwd20, mask.loc[test_dates])
        member_ics[name] = mi
        if (not member_gate) or (np.isfinite(mi["RankIC"]) and mi["RankIC"] >= 0.02):
            admitted[name] = sc
        else:
            print(f"M10-03: member {name} NOT admitted (RankIC {mi['RankIC']:.4f})",
                  flush=True)
    if not admitted:
        admitted = dict(scores)  # fall back: report honestly, gates will kill
    ens = ensemble_rank({k: v.loc[test_dates] for k, v in admitted.items()},
                        mask.loc[test_dates])
    dec = deciles(ens, mask.loc[test_dates])

    gross_mult = None
    if regime_on:
        gm = regime_multiplier(idx_blk, float(cfg.regime.vol_threshold_ann),
                               float(cfg.regime.gross_multiplier_risk_off))
        gross_mult = gm.reindex(test_dates)

    beta = idx_blk["beta"].reindex(test_dates) if "beta" in idx_blk else None
    targets = construct_targets(
        dec, mask.loc[test_dates], sigma32.loc[test_dates], beta, gross_mult,
        tranches=int(cfg.port.tranches), single_name_cap=float(cfg.port.single_name_cap),
        no_trade_band=float(cfg.port.no_trade_band),
        nav_band=float(cfg.port.no_trade_band_nav_bps) / 1e4)
    open_panel = panel.adj_open.loc[test_dates].copy()
    if "SPY" in open_panel.columns:                    # SPY is itself a universe member
        open_panel["SPY"] = open_panel["SPY"].fillna(spy["adj_open"].reindex(test_dates))
    else:
        open_panel["SPY"] = spy["adj_open"].reindex(test_dates)
    tgt = targets.copy()
    if HEDGE_COL in tgt.columns:
        hedge = tgt.pop(HEDGE_COL)
        tgt["SPY"] = (tgt["SPY"].fillna(0.0) if "SPY" in tgt.columns else 0.0) + hedge

    results = {}
    for bps in list(cfg.cost.sensitivity_bps):
        cm = CostModel(per_trade_bps=float(bps), slippage_bps=float(cfg.cost.slippage_bps),
                       borrow_gc_bps_yr=float(cfg.cost.borrow_gc_bps_yr))
        pre = run_backtest(tgt, open_panel, cm, tax_rate=cfg.tax.ordinary_rate)
        scale = vol_target_scale(pre.daily_net, float(cfg.port.vol_target_ann),
                                 float(cfg.port.vol_target_scale_cap))
        results[bps] = run_backtest(tgt.mul(scale.reindex(tgt.index).fillna(1.0), axis=0),
                                    open_panel, cm, tax_rate=cfg.tax.ordinary_rate)
    res15 = results.get(15, list(results.values())[0])

    ic = ic_summary(ens, fwd20, mask.loc[test_dates])
    cm15 = CostModel(per_trade_bps=15.0, borrow_gc_bps_yr=float(cfg.cost.borrow_gc_bps_yr))
    bl_spy = spy_buy_hold(spy["adj_open"].reindex(test_dates).dropna(), cm15)
    bl_mom = plain_momentum(panel.adj_close.loc[test_dates], panel.adj_open.loc[test_dates],
                            mask.loc[test_dates], cm15,
                            raw_close=panel.raw_close.loc[test_dates])
    perf = perf_summary(res15.daily_net, tgt, res15.daily_after_tax)
    beats_spy = sharpe(res15.daily_net) > sharpe(bl_spy)
    beats_mom = sharpe(res15.daily_net) > sharpe(bl_mom)
    dsr = deflated_sharpe(res15.daily_net.values, n_trials=ledger.n_trials(),
                          sr_trials_var=ledger.sr_variance())
    gates = evaluate_gates(ic["RankIC"], perf["sharpe_net"], perf["mdd"],
                           beats_spy, beats_mom)
    best_single = max((v["RankIC"] for v in member_ics.values()
                       if np.isfinite(v["RankIC"])), default=float("nan"))

    report = {
        "stage": stage,
        "stamp": artifact_stamp(config_hash, m1.data_snapshot_id(), git_sha(), seed),
        "universe": {"names": int(mask.any().sum()),
                     "avg_daily": float(mask.sum(axis=1).mean())},
        "folds": fold_stats or [],
        "members": member_ics,
        "ensemble_vs_best_single": {"ensemble_rank_ic": ic["RankIC"],
                                    "best_single_rank_ic": best_single,
                                    "ensemble_geq_best": bool(ic["RankIC"] >= best_single)
                                    if np.isfinite(best_single) else None},
        "ic_ensemble_vs_fwd20": ic,
        "performance_15bps": perf,
        "sensitivity": {str(b): perf_summary(r.daily_net) for b, r in results.items()},
        "baselines": {"spy_bh": perf_summary(bl_spy), "mom_12_1": perf_summary(bl_mom),
                      "alpha_beta_vs_spy": alpha_beta(res15.daily_net, bl_spy),
                      "alpha_beta_vs_mom": alpha_beta(res15.daily_net, bl_mom),
                      "beats_spy": bool(beats_spy), "beats_mom": bool(beats_mom)},
        "dsr": dsr,
        "gates": gates,
    }
    (out / f"{stage}_report.json").write_text(json.dumps(report, indent=2, default=str))
    def _save(frame, path):
        f = frame.copy()
        f.columns = [str(c) for c in f.columns]
        f.to_parquet(path)
    for k, v in scores.items():
        _save(v.loc[test_dates], out / f"scores_{k}.parquet")
    _save(ens, out / "ensemble_rank.parquet")
    _save(tgt, out / "target_weights.parquet")
    res15.daily_net.to_frame("net").to_parquet(out / "daily_net_15bps.parquet")
    print(json.dumps({"gates": gates, "perf": perf, "ic": ic}, indent=2, default=str),
          flush=True)
    return report
