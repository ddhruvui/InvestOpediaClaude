"""Stage 2 — ensemble & deep signals (§7, BP8-BP11).

Adds to Stage 1: M7 GRU heads on the top-20 LGBM features (per fold/head), the
M8 CNN chart-image signal (I5/R20, train-once, 5-retraining average), optional
F9 FinBERT sentiment features. Full 7-member rank ensemble with the M10-03
member-admission gate; Stage-2 gate: ensemble >= best single member.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import load_config
from src.pipeline.common import prepare, evaluate_book
from src.models.lgbm import LGBMHead
from src.models.cnn.render import render_windows
from src.primitives.fwd import forward_return
# torch-touching modules import lazily inside run_stage2: importing torch at module
# scope before the pyarrow/pandas-heavy prep segfaults on macOS (native-lib order)
from src.validation.splits import walk_forward
from src.hpo.determinism import seed_everything
from src.hpo.ledger import TrialsLedger


def run_stage2(m1_dir: str, eod_dir: str, out_dir: str, config_path: str | None = None,
               max_tickers: int | None = None, market_dir: str | None = None,
               finbert_dir: str | None = None) -> dict:
    cfg, config_hash = load_config(config_path)
    seed = int(cfg.seed["global"])
    seed_everything(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    finbert_dir = finbert_dir or os.environ.get("FINBERT_DIR", "/workspace/data_finbert")
    with_sent = Path(finbert_dir).exists()

    d = prepare(cfg, m1_dir, eod_dir, market_dir, max_tickers,
                with_sentiment=with_sent, finbert_dir=finbert_dir if with_sent else None,
                sent_cache=str(out / "sentiment_scores.parquet"))
    if d["health"]["blocking"]:
        raise RuntimeError(f"Q-004 BLOCKING failure: {d['health']}")
    panel, mask, feats, ys, labels = d["panel"], d["mask"], d["feats"], d["ys"], d["labels"]
    dates = panel.dates
    label_span = 1 + max(cfg.labels.horizons)
    folds = walk_forward(dates, train_sessions=252 * int(cfg.val.train_years),
                         valid_sessions=252 * int(cfg.val.valid_years),
                         test_sessions=252 * int(cfg.val.step_years),
                         step_sessions=252 * int(cfg.val.step_years),
                         label_span=label_span, embargo=int(cfg.val.embargo_days))
    print(f"walk-forward folds: {len(folds)}", flush=True)

    ledger = TrialsLedger()
    members = [f"lgbm_h{n}" for n in cfg.labels.horizons] \
        + [f"gru_h{n}" for n in cfg.labels.horizons] + ["cnn_I5R20"]
    scores = {mname: pd.DataFrame(np.nan, index=dates, columns=panel.tickers)
              for mname in members}
    fold_stats = []
    f_dates = feats.index.get_level_values("date")

    # ---- M8 CNN: train ONCE on the first fold's training block (M8-01) ----
    img_days = 5                                             # deployed I5/R20 (M8-02)
    up20 = (forward_return(panel.adj_close, int(cfg.cnn.supervision_horizon)) > 0) \
        .astype(float).where(mask)
    first_train = folds[0].train
    cut = int(len(first_train) * 0.7)                        # JKX 70/30 split
    print("rendering CNN training images (first fold)...", flush=True)
    imgs_tr = render_windows(panel.adj_open, panel.adj_high, panel.adj_low,
                             panel.adj_close, panel.adj_volume, first_train[:cut],
                             panel.tickers, img_days)
    imgs_va = render_windows(panel.adj_open, panel.adj_high, panel.adj_low,
                             panel.adj_close, panel.adj_volume, first_train[cut:],
                             panel.tickers, img_days)
    from src.models.gru import SequenceStore, make_sequences, train_gru, predict_gru
    from src.models.cnn.net import train_cnn, predict_cnn
    cnn_models = train_cnn(imgs_tr, imgs_va, up20, img_days, cfg, seed)
    del imgs_tr, imgs_va
    ledger.append("cnn_I5R20", {"train_policy": "train_once"}, config_hash,
                  "walkforward_fit", float("nan"), note="stage2")

    for fold in folds:
        tr_m, va_m, te_m = (f_dates.isin(fold.train), f_dates.isin(fold.valid),
                            f_dates.isin(fold.test))
        Xtr, Xva, Xte = feats[tr_m], feats[va_m], feats[te_m]
        for n in cfg.labels.horizons:
            ytr = ys[n].reindex(Xtr.index)
            yva = ys[n].reindex(Xva.index)
            ok_tr, ok_va = ytr.notna(), yva.notna()
            head = LGBMHead(cfg, n, seed).fit(Xtr[ok_tr.values], ytr[ok_tr],
                                              Xva[ok_va.values], yva[ok_va])
            p = head.predict(Xte)
            scores[f"lgbm_h{n}"].loc[fold.test] = \
                p.unstack("ticker").reindex(index=fold.test, columns=panel.tickers)
            top20 = head.top_importance(int(cfg.gru.n_features))
            ric = head.evals["valid"]["rank_ic"][head.booster.best_iteration - 1]
            fold_stats.append({"fold": fold.tag, "member": f"lgbm_h{n}",
                               "valid_rank_ic": float(ric)})
            ledger.append(f"lgbm_h{n}", {"fold": fold.tag}, config_hash,
                          "walkforward_fit", ric, note="stage2")

            # ---- M7 GRU on this head's top-20 (M6->M7 contract) ----
            store = SequenceStore(feats[[c for c in top20]], top20,
                                  lookback=int(cfg.gru.lookback))
            seqs_tr = make_sequences(store, fold.train)
            seqs_va = make_sequences(store, fold.valid)
            gmods, ginfo = train_gru(seqs_tr, seqs_va, labels[n], len(top20), cfg, seed)
            gp = predict_gru(gmods, make_sequences(store, fold.test), panel.tickers)
            scores[f"gru_h{n}"].loc[fold.test] = gp.reindex(index=fold.test,
                                                            columns=panel.tickers)
            gric = float(np.mean([s["best_valid_ric"] for s in ginfo["seeds"]]))
            fold_stats.append({"fold": fold.tag, "member": f"gru_h{n}",
                               "valid_rank_ic": gric})
            ledger.append(f"gru_h{n}", {"fold": fold.tag}, config_hash,
                          "walkforward_fit", gric, note="stage2")
            del store, seqs_tr, seqs_va

        # ---- CNN inference on this fold's test dates ----
        imgs_te = render_windows(panel.adj_open, panel.adj_high, panel.adj_low,
                                 panel.adj_close, panel.adj_volume, fold.test,
                                 panel.tickers, img_days)
        cp = predict_cnn(cnn_models, imgs_te, panel.tickers)
        scores["cnn_I5R20"].loc[fold.test] = cp.reindex(index=fold.test,
                                                        columns=panel.tickers)
        del imgs_te
        print(f"  {fold.tag} done", flush=True)

    report = evaluate_book(cfg, d, scores, folds, out, config_hash, seed,
                           ledger=ledger, fold_stats=fold_stats,
                           stage="stage2", member_gate=True)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default=os.environ.get("M1_DIR", "/workspace/m1"))
    ap.add_argument("--eod", default=os.environ.get("EOD_DIR", "/workspace/data"))
    ap.add_argument("--out", default=os.environ.get("OUT_DIR", "artifacts/reports/stage2"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-tickers", type=int, default=None)
    ap.add_argument("--market", default=os.environ.get("MARKET_DIR") or None)
    ap.add_argument("--finbert", default=None)
    args = ap.parse_args()
    run_stage2(args.m1, args.eod, args.out, args.config, args.max_tickers,
               market_dir=args.market, finbert_dir=args.finbert)
