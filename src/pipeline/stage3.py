"""Stage 3 — harden, gate, operate (§7, BP12-BP16).

Adds: M5.2 barrier exits live in the backtest (event engine), M11 meta gate with
the M11-02 adoption comparison (gated vs ungated on identical folds), CPCV(6,2)
path distribution + DSR over the trials ledger.

Meta training is strictly causal: the meta model for fold k trains on the
OUT-OF-SAMPLE candidates of folds < k (their barrier outcomes net of costs);
fold 0 runs ungated. [IMPL reading of M11-04 "trains strictly after the primary".]
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
from src.backtest.costs import CostModel
from src.backtest.engines.barriers_event import run_event_backtest
from src.meta.gate import (candidates_from_deciles, meta_context, meta_outcomes,
                           train_meta, meta_multiplier, META_FEATURES)
from src.primitives.monthly import mom_12_1
from src.primitives.rolling import RollingCache
from src.primitives.returns import daily_return
from src.regime.overlay import regime_multiplier
from src.validation.splits import walk_forward, cpcv
from src.validation.metrics import perf_summary, sharpe, max_drawdown, ic_summary
from src.validation.dsr import deflated_sharpe
from src.hpo.determinism import seed_everything, artifact_stamp
from src.hpo.ledger import TrialsLedger


def _load_scores(scores_dir: Path, members: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for m in members:
        p = scores_dir / f"scores_{m}.parquet"
        if p.exists():
            out[m] = pd.read_parquet(p)
    return out


def run_stage3(m1_dir: str, eod_dir: str, out_dir: str, scores_dir: str,
               config_path: str | None = None, max_tickers: int | None = None,
               market_dir: str | None = None, account_equity: float | None = None,
               do_cpcv: bool = True) -> dict:
    cfg, config_hash = load_config(config_path)
    seed = int(cfg.seed["global"])
    seed_everything(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    d = prepare(cfg, m1_dir, eod_dir, market_dir, max_tickers)
    panel, mask, sigma32 = d["panel"], d["mask"], d["sigma32"]
    dates = panel.dates
    ledger = TrialsLedger()
    cm = CostModel(per_trade_bps=float(cfg.cost.per_trade_bps),
                   borrow_gc_bps_yr=float(cfg.cost.borrow_gc_bps_yr),
                   borrow_table=d["m1"].borrow_fees() if hasattr(d["m1"], "borrow_fees")
                   else None)

    members = [f"lgbm_h{n}" for n in cfg.labels.horizons] \
        + [f"gru_h{n}" for n in cfg.labels.horizons] + ["cnn_I5R20"]
    scores = _load_scores(Path(scores_dir), members)
    if not scores:
        raise FileNotFoundError(f"no scores_*.parquet under {scores_dir} — "
                                "run stage1/stage2 first")
    print(f"loaded members: {list(scores)}", flush=True)
    label_span = 1 + max(cfg.labels.horizons)
    folds = walk_forward(dates, train_sessions=252 * int(cfg.val.train_years),
                         valid_sessions=252 * int(cfg.val.valid_years),
                         test_sessions=252 * int(cfg.val.step_years),
                         step_sessions=252 * int(cfg.val.step_years),
                         label_span=label_span, embargo=int(cfg.val.embargo_days))
    test_dates = pd.DatetimeIndex(sorted(set().union(*[set(f.test) for f in folds])))
    test_dates = test_dates.intersection(next(iter(scores.values())).dropna(how="all").index)

    ens = ensemble_rank({k: v.reindex(test_dates) for k, v in scores.items()},
                        mask.loc[test_dates])
    dec = deciles(ens, mask.loc[test_dates])
    gm = regime_multiplier(d["idx_blk"], float(cfg.regime.vol_threshold_ann),
                           float(cfg.regime.gross_multiplier_risk_off))

    # context features for the meta model
    r = d["r"]
    cache = RollingCache({"r": r})
    vol20 = cache.get("r", "std", 20)
    mom = mom_12_1(panel.adj_close)
    size_f = None                                             # ln(mcap) optional

    # ---------------- ungated book (barrier exits, event engine) ----------------
    # M13 regime overlay halves the entering tranche on risk-off days; M14 vol
    # targeting scales it by min(target/EWMA vol of the pre-run book, cap),
    # causally (pre-run pass -> scale -> final run).
    from src.portfolio.construct import vol_target_scale
    sel_ungated = dec.eq(10)
    gm_series = gm.reindex(test_dates).fillna(1.0)
    pre = run_event_backtest(sel_ungated, panel, sigma32, cm, cfg,
                             day_budget_mult=gm_series,
                             account_equity=account_equity)
    vt = vol_target_scale(pre["daily_net"], float(cfg.port.vol_target_ann),
                          float(cfg.port.vol_target_scale_cap))
    budget = (gm_series * vt.reindex(test_dates).fillna(1.0)).clip(lower=0.0)
    res_ungated = run_event_backtest(sel_ungated, panel, sigma32, cm, cfg,
                                     day_budget_mult=budget,
                                     account_equity=account_equity)

    # ---------------- meta gate, causally per fold (M11-04) ----------------
    meta_mult = pd.DataFrame(np.nan, index=test_dates, columns=panel.tickers)
    train_pool: list[pd.DataFrame] = []
    outcomes_pool: list[pd.DataFrame] = []
    meta_stats = []
    for k, fold in enumerate(folds):
        fold_test = pd.DatetimeIndex(fold.test).intersection(test_dates)
        if not len(fold_test):
            continue
        cand = candidates_from_deciles(dec, fold_test)
        if k > 0 and train_pool:
            tr_cand = pd.concat(train_pool, ignore_index=True)
            tr_out = pd.concat(outcomes_pool, ignore_index=True)
            ok = tr_out["y"].notna() & tr_out["barrier_hit"].isin(
                ["upper", "lower", "vertical"])
            if ok.sum() > 200:      # [IMPL] minimum meta training set
                X = meta_context(tr_cand[ok.values.tolist()], ens, sigma32, vol20, mom,
                                 size_f, gm, None, None)
                mdl = train_meta(X, tr_out.loc[ok, "y"], cfg, seed)
                Xte = meta_context(cand, ens, sigma32, vol20, mom, size_f, gm, None, None)
                p = pd.Series(mdl.predict(Xte.values), index=cand.index)
                mm = meta_multiplier(p, float(cfg.meta.p_threshold),
                                     str(cfg.meta.sizing))
                for (i, row), v in zip(cand.iterrows(), mm):
                    meta_mult.at[row["date"], row["ticker"]] = v
                meta_stats.append({"fold": fold.tag, "n_train": int(ok.sum()),
                                   "mean_p": float(p.mean()),
                                   "gated_frac": float((mm > 0).mean())})
                ledger.append("meta", {"fold": fold.tag}, config_hash,
                              "meta_fit", float(p.mean()), note="stage3")
        # this fold's candidates + outcomes join the future training pool
        oc = meta_outcomes(cand, panel, sigma32, cm, cfg)
        train_pool.append(cand)
        outcomes_pool.append(oc)
        print(f"  {fold.tag}: {len(cand)} candidates pooled", flush=True)

    # dates where no meta model existed yet (fold 0 / thin pools) pass through
    # UNGATED: the M11-02 comparison is then driven by the genuinely gated dates.
    has_meta = meta_mult.notna().any(axis=1)
    mm_filled = meta_mult.copy()
    mm_filled.loc[~has_meta] = 1.0
    sel_gated = sel_ungated & (mm_filled.fillna(0.0) > 0)
    res_gated = run_event_backtest(sel_gated, panel, sigma32, cm,
                                   cfg, meta_mult=mm_filled,
                                   day_budget_mult=budget,
                                   account_equity=account_equity)

    # ---------------- M11-02 adoption gate ----------------
    s_un, s_gt = sharpe(res_ungated["daily_net"]), sharpe(res_gated["daily_net"])
    to_un = res_ungated["n_trades"]
    to_gt = res_gated["n_trades"]
    adoption = {"ungated": {"sharpe": s_un, "n_trades": to_un,
                            "mdd": max_drawdown(res_ungated["daily_net"]),
                            **{k: res_ungated[k] for k in ("avg_hold", "hit_counts")}},
                "gated": {"sharpe": s_gt, "n_trades": to_gt,
                          "mdd": max_drawdown(res_gated["daily_net"]),
                          **{k: res_gated[k] for k in ("avg_hold", "hit_counts")}},
                "turnover_cut": 1 - to_gt / max(1, to_un),
                "adopt": bool(s_gt >= s_un and to_gt < 0.8 * to_un)}
    print(f"M11-02: ungated SR {s_un:.2f}/{to_un} trades; "
          f"gated SR {s_gt:.2f}/{to_gt} trades; adopt={adoption['adopt']}", flush=True)

    # ---------------- CPCV(6,2) on LGBM heads (model selection machinery) ------
    cpcv_report = None
    if do_cpcv:
        feats, ys = d["feats"], d["ys"]
        f_dates = feats.index.get_level_values("date")
        splits, path_map = cpcv(dates, int(cfg.val.cpcv_n_groups),
                                int(cfg.val.cpcv_k_test), label_span,
                                int(cfg.val.embargo_days))
        split_scores: dict[int, pd.DataFrame] = {}
        for ci, sp in enumerate(splits):
            tr_m, te_m = f_dates.isin(sp.train), f_dates.isin(sp.test)
            Xtr, Xte = feats[tr_m], feats[te_m]
            per_member = {}
            for n in cfg.labels.horizons:
                ytr = ys[n].reindex(Xtr.index)
                ok = ytr.notna()
                cut = int(ok.sum() * 0.9)
                head = LGBMHead(cfg, n, seed).fit(
                    Xtr[ok.values][:cut], ytr[ok][:cut],
                    Xtr[ok.values][cut:], ytr[ok][cut:])
                per_member[f"lgbm_h{n}"] = head.predict(Xte).unstack("ticker") \
                    .reindex(columns=panel.tickers)
                ledger.append(f"lgbm_h{n}", {"cpcv": sp.tag}, config_hash,
                              "cpcv_fit", float("nan"), note="stage3")
            e = ensemble_rank(per_member, mask.reindex(per_member["lgbm_h5"].index)
                              if "lgbm_h5" in per_member else mask)
            split_scores[ci] = e
            print(f"  cpcv split {ci + 1}/15 done", flush=True)
        # assemble 5 paths and run the event book on each
        n = len(dates)
        bounds = np.linspace(0, n, int(cfg.val.cpcv_n_groups) + 1).astype(int)
        path_stats = []
        for pi, prow in enumerate(path_map):
            segs = []
            for g, ci in enumerate(prow):
                gd = dates[bounds[g]:bounds[g + 1]]
                segs.append(split_scores[ci].reindex(gd))
            path_ens = pd.concat(segs)
            pdec = deciles(path_ens, mask.reindex(path_ens.index))
            pres = run_event_backtest(pdec.eq(10), panel, sigma32, cm, cfg)
            path_stats.append({"path": pi, "sharpe": sharpe(pres["daily_net"]),
                               "mdd": max_drawdown(pres["daily_net"]),
                               "n_trades": pres["n_trades"]})
            print(f"  path {pi}: SR {path_stats[-1]['sharpe']:.2f} "
                  f"MDD {path_stats[-1]['mdd']:.1%}", flush=True)
        cpcv_report = {"paths": path_stats,
                       "sharpe_median": float(np.median([p["sharpe"] for p in path_stats])),
                       "mdd_worst": float(min(p["mdd"] for p in path_stats))}

    book = res_gated if adoption["adopt"] else res_ungated
    dsr = deflated_sharpe(book["daily_net"].values, n_trials=ledger.n_trials(),
                          sr_trials_var=ledger.sr_variance())
    report = {"stage": "stage3",
              "stamp": artifact_stamp(config_hash, d["m1"].data_snapshot_id(),
                                      git_sha(), seed),
              "adoption": adoption, "meta_folds": meta_stats,
              "cpcv": cpcv_report, "dsr": dsr,
              "book": perf_summary(book["daily_net"])}
    (out / "stage3_report.json").write_text(json.dumps(report, indent=2, default=str))
    book["daily_net"].to_frame("net").to_parquet(out / "stage3_daily_net.parquet")

    # ---- suggested -> outcome ledger (what the book proposed and what happened) ----
    # Every barrier trade with the conviction that produced it, so the reporting
    # layer can answer "did the strongest suggestions actually work out?".
    for tag, res in (("ungated", res_ungated), ("gated", res_gated)):
        tr = res["trades"]
        if tr is None or not len(tr):
            continue
        tr = tr.copy()
        idx = list(zip(tr["entry_date"], tr["ticker"]))
        er = ens.stack(future_stack=True)
        tr["ensemble_rank"] = er.reindex(idx).to_numpy()
        if meta_mult is not None:
            mm = meta_mult.stack(future_stack=True)
            tr["meta_mult"] = mm.reindex(idx).to_numpy()
        tr["regime_mult"] = gm.reindex(tr["entry_date"]).to_numpy()
        tr.to_parquet(out / f"stage3_trades_{tag}.parquet", index=False)
        print(f"trades[{tag}]: {len(tr):,} rows -> stage3_trades_{tag}.parquet", flush=True)
    book["equity"].to_frame("equity").to_parquet(out / "stage3_equity.parquet")
    print(json.dumps({"adoption": adoption["adopt"], "cpcv": cpcv_report,
                      "dsr": dsr.get("DSR")}, indent=2, default=str), flush=True)
    return report


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default=os.environ.get("M1_DIR", "/workspace/m1"))
    ap.add_argument("--eod", default=os.environ.get("EOD_DIR", "/workspace/data"))
    ap.add_argument("--out", default=os.environ.get("OUT_DIR", "artifacts/reports/stage3"))
    ap.add_argument("--scores", default=os.environ.get("SCORES_DIR",
                                                       "/workspace/derived/stage1"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-tickers", type=int, default=None)
    ap.add_argument("--market", default=os.environ.get("MARKET_DIR") or None)
    ap.add_argument("--no-cpcv", action="store_true")
    ap.add_argument("--equity", type=float, default=None)
    args = ap.parse_args()
    run_stage3(args.m1, args.eod, args.out, args.scores, args.config, args.max_tickers,
               market_dir=args.market, do_cpcv=not args.no_cpcv,
               account_equity=args.equity)
