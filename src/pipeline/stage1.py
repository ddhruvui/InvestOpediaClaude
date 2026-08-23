"""Stage 1 — honest baseline (§7, BP1-BP7).

M1 -> M2 -> M3 -> M4 (F1-F6+F8) -> M5.1 -> purged walk-forward M6 (3 heads) ->
M10 (LGBM blend) -> M14 (fixed 15-tranche rotation, inverse-vol, caps, SPY hedge)
-> M15 (fast path, 15 bps + {5,30} sensitivity) -> M16 (metrics, baselines, gates).

Gate: G-11 minimums on walk-forward AND both baselines beaten -> proceed to Stage 2.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import load_config, git_sha
from src.pipeline.common import prepare
from src.models.lgbm import LGBMHead
from src.ensemble.rank import ensemble_rank, deciles
from src.portfolio.construct import construct_targets, vol_target_scale, HEDGE_COL
from src.backtest.costs import CostModel
from src.backtest.engine import run_backtest
from src.classical.momentum import momentum_targets
from src.regime.overlay import regime_multiplier
from src.validation.splits import walk_forward
from src.validation.metrics import ic_summary, perf_summary, alpha_beta, sharpe, max_drawdown
from src.validation.baselines import spy_buy_hold, plain_momentum
from src.validation.gates import evaluate_gates
from src.validation.dsr import deflated_sharpe
from src.hpo.determinism import seed_everything, artifact_stamp
from src.hpo.ledger import TrialsLedger


def _stack_y(labels: dict[int, pd.DataFrame]) -> dict[int, pd.Series]:
    out = {}
    for n, w in labels.items():
        s = w.stack(future_stack=True)
        s.index.names = ["date", "ticker"]
        out[n] = s
    return out


def run_stage1(m1_dir: str, eod_dir: str, out_dir: str,
               config_path: str | None = None, max_tickers: int | None = None,
               regime_on: bool = False, market_dir: str | None = None) -> dict:
    cfg, config_hash = load_config(config_path)
    seed = int(cfg.seed["global"])
    seed_everything(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # ---------------- M1/M2/M3/M4/M5 via the shared prep (G-15) ----------------
    d = prepare(cfg, m1_dir, eod_dir, market_dir, max_tickers)
    m1, panel, mask = d["m1"], d["panel"], d["mask"]
    sigma32, spy, idx_blk = d["sigma32"], d["spy"], d["idx_blk"]
    feats, manifest, ys = d["feats"], d["manifest"], d["ys"]
    health = d["health"]
    (out / "health_report.json").write_text(json.dumps(health, indent=2, default=str))
    if health["blocking"]:
        raise RuntimeError(f"Q-004 health check BLOCKING failure: {health}")
    (out / "feature_manifest.json").write_text(json.dumps(manifest, indent=2))

    # ---------------- M16 splits + M6 walk-forward ----------------
    dates = panel.dates
    label_span = 1 + max(cfg.labels.horizons)
    folds = walk_forward(dates, train_sessions=252 * int(cfg.val.train_years),
                         valid_sessions=252 * int(cfg.val.valid_years),
                         test_sessions=252 * int(cfg.val.step_years),
                         step_sessions=252 * int(cfg.val.step_years),
                         label_span=label_span, embargo=int(cfg.val.embargo_days))
    if not folds:
        raise RuntimeError(f"sample too short for walk-forward: {len(dates)} sessions")
    print(f"walk-forward folds: {len(folds)}")

    ledger = TrialsLedger()
    scores: dict[str, pd.DataFrame] = {f"lgbm_h{n}": pd.DataFrame(np.nan, index=dates,
                                       columns=panel.tickers) for n in cfg.labels.horizons}
    fold_stats = []
    importances: dict[int, list[str]] = {}
    f_dates = feats.index.get_level_values("date")
    for fold in folds:
        tr_m = f_dates.isin(fold.train)
        va_m = f_dates.isin(fold.valid)
        te_m = f_dates.isin(fold.test)
        Xtr, Xva, Xte = feats[tr_m], feats[va_m], feats[te_m]
        for n in cfg.labels.horizons:
            ytr = ys[n].reindex(Xtr.index)
            yva = ys[n].reindex(Xva.index)
            ok_tr, ok_va = ytr.notna(), yva.notna()
            head = LGBMHead(cfg, n, seed).fit(Xtr[ok_tr.values], ytr[ok_tr],
                                              Xva[ok_va.values], yva[ok_va])
            p = head.predict(Xte)
            w = p.unstack("ticker").reindex(columns=panel.tickers)
            scores[f"lgbm_h{n}"].loc[w.index] = w
            importances[n] = head.top_importance(int(cfg.gru.n_features))
            ric = head.evals["valid"]["rank_ic"][head.booster.best_iteration - 1]
            fold_stats.append({"fold": fold.tag, "horizon": n,
                               "valid_rank_ic": float(ric),
                               "best_iter": int(head.booster.best_iteration)})
            ledger.append(f"lgbm_h{n}", {"fold": fold.tag}, config_hash,
                          "walkforward_fit", ric, note="stage1")
        print(f"  {fold.tag}: " + ", ".join(
            f"h{s['horizon']} ric={s['valid_rank_ic']:.4f}" for s in fold_stats[-3:]))
    (out / "lgbm_top_features.json").write_text(json.dumps(importances, indent=2))

    # ---------------- M10 -> M14 -> M15 -> M16 (shared tail, G-15) ----------------
    from src.pipeline.common import evaluate_book
    report = evaluate_book(cfg, d, scores, folds, out, config_hash, seed,
                           ledger=ledger, fold_stats=fold_stats,
                           regime_on=regime_on, stage="stage1")
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default=os.environ.get("M1_DIR", "/workspace/m1"))
    ap.add_argument("--eod", default=os.environ.get("EOD_DIR", "/workspace/data"))
    ap.add_argument("--out", default=os.environ.get("OUT_DIR", "artifacts/reports/stage1"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-tickers", type=int, default=None)
    ap.add_argument("--market", default=os.environ.get("MARKET_DIR") or None,
                    help="m1x dir from build_market.py (whole-market universe)")
    args = ap.parse_args()
    run_stage1(args.m1, args.eod, args.out, args.config, args.max_tickers,
               market_dir=args.market)
