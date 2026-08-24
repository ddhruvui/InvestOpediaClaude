"""Daily prediction (§2.3 production sequence, research side).

CONTINUAL LEARNING: champions persist in a ModelStore on the volume. A daily run
warm-continues each champion on the newest labeled year (LightGBM init_model),
scores champion and challenger on the SAME purged validation window, and keeps
the better model — so learning accrues day over day instead of restarting. A
from-scratch full refit still happens every `continual.full_refit_sessions`
(= val.retrain_cadence) or whenever config/features change. In update mode only
a `panel_tail_years` slice of the panel is loaded, which is what keeps the
daily run short. Every fit, adopted or not, lands in the G-09 trials ledger.

Scores the most recent sessions, blends ranks, runs M14 over the trailing
window to warm the tranche state, and emits the target book for the NEXT open
plus a buy/sell suggestion report with M5.2 barrier levels for new entries.

Output is research tooling for the system's operator — not financial advice
(blueprint CAV: "nothing here guarantees profit").
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from src.config import load_config, git_sha
from src.data.m1 import M1
from src.pipeline.common import prepare
from src.models.lgbm import LGBMHead
from src.models.store import ModelStore, decide_refit
from src.ensemble.rank import ensemble_rank, deciles
from src.portfolio.construct import construct_targets, HEDGE_COL
from src.validation.splits import _purge_embargo
from src.hpo.determinism import seed_everything, artifact_stamp
from src.hpo.ledger import TrialsLedger

WARM_SESSIONS = 90            # trailing window that warms the 15-tranche rotation


def run_predict(m1_dir: str, eod_dir: str, out_dir: str, config_path: str | None = None,
                max_tickers: int | None = None, market_dir: str | None = None,
                positions_csv: str | None = None, model_dir: str | None = None,
                refit: str | None = None) -> dict:
    cfg, config_hash = load_config(config_path)
    seed = int(cfg.seed["global"])
    seed_everything(seed)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        cc = cfg.continual
    except AttributeError:
        cc = None
    store = ModelStore(model_dir or os.environ.get("MODEL_DIR")
                       or (cc.model_dir if cc else "models"))
    sessions_all = M1(m1_dir).sessions()
    mode, why = decide_refit(cfg, store, config_hash, sessions_all, cfg.labels.horizons,
                             forced=refit or os.environ.get("REFIT"))
    since = None
    if mode == "update":
        i_now = int(sessions_all.searchsorted(pd.Timestamp.today().normalize(),
                                              side="right"))
        tail = int(252 * float(cc.panel_tail_years))
        since = sessions_all[max(0, i_now - tail)]
    print(f"refit mode: {mode} — {why}"
          + (f" | panel tail since {since.date()}" if since is not None else ""),
          flush=True)

    d = prepare(cfg, m1_dir, eod_dir, market_dir, max_tickers, since=since)

    champs = {}
    if mode == "update":
        for n in cfg.labels.horizons:
            champs[n] = store.load_champion(cfg, n, seed)
        feat_cols = list(d["feats"].columns)
        bad = [n for n, c in champs.items()
               if c is None or c[1]["feature_names"] != feat_cols]
        if bad:
            # feature set drifted since the champions were trained — a warm continue
            # would silently mis-map columns, so fall back to a full refit on full data
            print(f"champion/feature mismatch on heads {bad} — falling back to full "
                  f"refit", flush=True)
            mode, why, champs = "full", "feature set changed after prep", {}
            if since is not None:
                del d
                import gc
                gc.collect()
                since = None
                d = prepare(cfg, m1_dir, eod_dir, market_dir, max_tickers)

    if d["health"]["blocking"]:
        raise RuntimeError(f"Q-004 BLOCKING failure: {d['health']}")
    panel, mask, feats, ys = d["panel"], d["mask"], d["feats"], d["ys"]
    sigma32, spy, idx_blk = d["sigma32"], d["spy"], d["idx_blk"]
    dates = panel.dates
    label_span = 1 + max(cfg.labels.horizons)
    embargo = int(cfg.val.embargo_days)

    # ---- final training fold: labeled history, last year = early-stop valid ----
    # full mode trains on everything before the valid year; update mode warm-continues
    # the champion on only the newest `update_train_sessions` of that zone (purged the
    # same way), which with the tail panel is what makes the daily run cheap
    n_labeled = len(dates) - label_span
    valid_iv = [(n_labeled - 252, n_labeled - 1)]
    zone_end = n_labeled - 252
    zone_start = max(0, zone_end - int(cc.update_train_sessions)) if mode == "update" \
        else 0
    tr_idx = _purge_embargo(dates, np.arange(zone_start, zone_end), valid_iv,
                            label_span, 0)
    train_dates, valid_dates = dates[tr_idx], dates[n_labeled - 252:n_labeled]
    f_dates = feats.index.get_level_values("date")
    Xtr = feats[f_dates.isin(train_dates)]
    Xva = feats[f_dates.isin(valid_dates)]
    score_dates = dates[-WARM_SESSIONS:]
    Xsc = feats[f_dates.isin(score_dates)]

    ledger = TrialsLedger()
    train_through = str(pd.Timestamp(dates[n_labeled - 1]).date())
    scores = {}
    heads_info = {}
    for n in cfg.labels.horizons:
        ytr, yva = ys[n].reindex(Xtr.index), ys[n].reindex(Xva.index)
        ok_tr, ok_va = ytr.notna(), yva.notna()
        Xva_ok, yva_ok = Xva[ok_va.values], yva[ok_va]
        stamp = artifact_stamp(config_hash, d["m1"].data_snapshot_id(), git_sha(), seed)
        if mode == "update":
            champ_head, champ_meta = champs[n]
            head = LGBMHead(cfg, n, seed).fit(
                Xtr[ok_tr.values], ytr[ok_tr], Xva_ok, yva_ok,
                init_booster=champ_head.booster,
                num_boost_round=int(cc.update_boost_rounds),
                learning_rate=float(cc.update_learning_rate))
            ric_new = head.valid_rank_ic(Xva_ok, yva_ok)
            ric_champ = champ_head.valid_rank_ic(Xva_ok, yva_ok)
            adopted = bool(np.isfinite(ric_new)
                           and (not np.isfinite(ric_champ) or ric_new > ric_champ))
            ledger.append(f"lgbm_h{n}", {"mode": "warm_update",
                                         "parent": champ_meta["model_file"],
                                         "train_through": train_through},
                          config_hash, "valid_rank_ic", ric_new,
                          note="predict_warm_update"
                               + ("_adopted" if adopted else "_rejected"))
            if adopted:
                store.save_champion(head, {
                    "mode": "warm_update", "parent": champ_meta["model_file"],
                    "train_through": train_through,
                    "full_train_through": champ_meta["full_train_through"],
                    "valid_rank_ic": ric_new, "config_hash": config_hash,
                    "stamp": stamp}, keep_files=int(cc.keep_model_files))
            else:
                head = champ_head
            heads_info[f"lgbm_h{n}"] = {
                "mode": "warm_update", "adopted": adopted,
                "valid_rank_ic": ric_new if adopted else ric_champ,
                "challenger_rank_ic": ric_new, "champion_rank_ic": ric_champ,
                "n_trees": int(head.booster.num_trees()),
                "top_features": head.top_importance(10)}
            print(f"h{n}: champion {ric_champ:.4f} vs challenger {ric_new:.4f} -> "
                  f"{'ADOPTED' if adopted else 'kept champion'}", flush=True)
        else:
            head = LGBMHead(cfg, n, seed).fit(Xtr[ok_tr.values], ytr[ok_tr],
                                              Xva_ok, yva_ok)
            ric = head.valid_rank_ic(Xva_ok, yva_ok)
            ledger.append(f"lgbm_h{n}", {"mode": "full_refit",
                                         "train_through": train_through},
                          config_hash, "valid_rank_ic", ric, note="predict_full_refit")
            if cc is not None and bool(cc.enabled):
                store.save_champion(head, {
                    "mode": "full_refit", "parent": None,
                    "train_through": train_through,
                    "full_train_through": train_through,
                    "valid_rank_ic": ric, "config_hash": config_hash,
                    "stamp": stamp}, keep_files=int(cc.keep_model_files))
            heads_info[f"lgbm_h{n}"] = {
                "mode": "full_refit", "adopted": True,
                "valid_rank_ic": ric,
                "best_iter": int(head.booster.best_iteration),
                "top_features": head.top_importance(10)}
            print(f"h{n}: valid RIC {ric:.4f}", flush=True)
        p = head.predict(Xsc)
        scores[f"lgbm_h{n}"] = p.unstack("ticker").reindex(index=score_dates,
                                                           columns=panel.tickers)

    # ---- live-tradability screen [IMPL]: the centered-window tape hygiene of
    # build_panel cannot protect the LAST bars (no future context), so the
    # prediction-time selection additionally requires: a live Sharadar entity
    # (isdelisted == N), a fresh tape (traded within 2 sessions), a last close
    # inside [0.25x, 4x] of its trailing 21d median, and a sane sigma32 (>= 0.5%
    # daily — stale tapes fake near-zero vol and grab outsized inverse-vol
    # weights). Applies only to the live path, never to backtests. ----
    ents = d["m1"].entities()
    live_ok = set(ents.loc[ents.get("isdelisted", "N").astype(str) != "Y",
                           "ticker"].astype(str))
    live_ok |= {t.replace(".", "-") for t in live_ok}
    t_last_all = panel.dates[-1]
    trail_med = panel.raw_close.rolling(21, min_periods=10).median().iloc[-1]
    lvl_ratio = panel.raw_close.iloc[-1] / trail_med
    fresh = panel.raw_close.iloc[-3:].notna().any()
    sane_sigma = sigma32.iloc[-1] >= 0.005
    tradable = (pd.Series([str(t) in live_ok for t in panel.tickers],
                          index=panel.tickers)
                & lvl_ratio.between(0.25, 4.0) & fresh & sane_sigma)
    n_dropped = int((~tradable).sum())
    print(f"live screen: {n_dropped} names not currently tradable-quality "
          f"(delisted/stale/level-shifted/zero-vol)", flush=True)
    mask = mask & pd.DataFrame(
        np.broadcast_to(tradable.values, (len(mask), len(tradable))),
        index=mask.index, columns=mask.columns)

    # ---- ensemble -> deciles -> warm M14 -> final target row ----
    m = mask.loc[score_dates]
    ens = ensemble_rank(scores, m)
    dec = deciles(ens, m)
    beta = idx_blk["beta"].reindex(score_dates) if "beta" in idx_blk else None
    targets = construct_targets(
        dec, m, sigma32.loc[score_dates], beta, None,
        tranches=int(cfg.port.tranches), single_name_cap=float(cfg.port.single_name_cap),
        no_trade_band=float(cfg.port.no_trade_band),
        nav_band=float(cfg.port.no_trade_band_nav_bps) / 1e4)
    t_last = score_dates[-1]
    w = targets.iloc[-1]
    hedge_w = float(w.get(HEDGE_COL, 0.0))
    w = w.drop(labels=[HEDGE_COL], errors="ignore")
    book = w[w.abs() > 1e-6].sort_values(ascending=False)

    # ---- barrier levels for entries (M5.2 parameters, priced off last close) ----
    m_bar, h_bar = float(cfg.barrier.m), int(cfg.barrier.h_days)
    thr = m_bar * sigma32.loc[t_last] * np.sqrt(h_bar)
    cap = cfg.barrier.get("thr_cap_pct")
    if cap is not None:
        thr = thr.clip(upper=float(cap))
    last_close = panel.raw_close.loc[t_last]
    ens_last = ens.loc[t_last]

    # ---- diff vs current positions ----
    current = pd.Series(dtype=float)
    if positions_csv and Path(positions_csv).exists():
        pos = pd.read_csv(positions_csv)
        current = pos.set_index("ticker")["weight"] if "weight" in pos else \
            pd.Series(dtype=float)
    buys, sells, holds = [], [], []
    for t, wt in book.items():
        cur = float(current.get(t, 0.0))
        row = {"ticker": t, "target_weight": round(float(wt), 5),
               "current_weight": round(cur, 5),
               "ensemble_rank": round(float(ens_last.get(t, np.nan)), 4),
               "last_close": round(float(last_close.get(t, np.nan)), 2),
               "stop_pct": round(-float(thr.get(t, np.nan)) * 100, 2),
               "profit_take_pct": round(float(thr.get(t, np.nan)) * 100, 2),
               "max_hold_sessions": h_bar}
        (buys if wt > cur + 1e-6 else holds).append(row)
    for t, cur in current.items():
        if t not in book.index and abs(cur) > 1e-6:
            sells.append({"ticker": t, "target_weight": 0.0,
                          "current_weight": round(float(cur), 5)})

    suggestions = {
        "as_of_close": str(t_last.date()),
        "execute_at": "next session MOO (G-02: signals from close t earn from open t+1)",
        "stamp": artifact_stamp(config_hash, d["m1"].data_snapshot_id(), git_sha(), seed),
        "training": {
            "mode": mode, "reason": why,
            "train_through": train_through,
            "panel_tail_since": str(since.date()) if since is not None else None,
            "adopted": {k: bool(v.get("adopted", True)) for k, v in heads_info.items()},
            "full_refit_cadence_sessions": int(cc.full_refit_sessions) if cc else None,
            "note": "warm_update continues the stored champion on the newest labeled "
                    "year; champion vs challenger judged on the same purged valid year",
        },
        "heads": heads_info,
        "portfolio": {"n_names": int(len(book)), "gross_long": float(book.clip(lower=0).sum()),
                      "spy_hedge_weight": round(hedge_w, 4)},
        "buys_or_increases": buys,
        "holds": holds,
        "sells_or_exits": sells,
        "exit_rules": {"engine": "M5.2 triple barrier", "m": m_bar, "h_sessions": h_bar,
                       "note": "stop/profit-take are % vs ACTUAL fill at next open; "
                               "vertical exit = MOO at t+h+1"},
        "disclaimer": "Research output of an experimental system; NOT financial advice. "
                      "All performance claims require the G-11 gate evaluation first.",
    }
    (out / "suggestions.json").write_text(json.dumps(suggestions, indent=2, default=str))

    md = [f"# Target book for next open (signals @ close {t_last.date()})", "",
          f"- Names: {len(book)}, gross long {book.clip(lower=0).sum():.2f}, "
          f"SPY hedge {hedge_w:+.2f}",
          f"- Heads valid RankIC: " + ", ".join(
              f"{k} {v['valid_rank_ic']:.3f}" for k, v in heads_info.items()),
          f"- Training: {mode} ({why})" + ("" if mode != "warm_update" and mode != "update"
              else " — " + ", ".join(
                  f"{k} {'adopted' if v.get('adopted') else 'kept champion'}"
                  for k, v in heads_info.items())), "",
          "| ticker | action | target w | rank | last close | stop % | PT % |",
          "|---|---|---|---|---|---|---|"]
    for row in buys[:40]:
        md.append(f"| {row['ticker']} | BUY/ADD | {row['target_weight']:.3%} | "
                  f"{row['ensemble_rank']:+.3f} | {row['last_close']} | "
                  f"{row['stop_pct']}% | +{row['profit_take_pct']}% |")
    for row in sells[:20]:
        md.append(f"| {row['ticker']} | EXIT | 0 |  |  |  |  |")
    md += ["", "_Vertical exit: MOO " + str(h_bar) + " sessions after entry. "
           "Research tooling, not financial advice._"]
    (out / "suggestions.md").write_text("\n".join(md))
    print(f"suggestions written: {len(buys)} buys/adds, {len(sells)} exits, "
          f"{len(holds)} holds", flush=True)
    return suggestions


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1", default=os.environ.get("M1_DIR", "/workspace/m1"))
    ap.add_argument("--eod", default=os.environ.get("EOD_DIR", "/workspace/data"))
    ap.add_argument("--out", default=os.environ.get("OUT_DIR", "artifacts/reports/predict"))
    ap.add_argument("--config", default=None)
    ap.add_argument("--max-tickers", type=int, default=None)
    ap.add_argument("--market", default=os.environ.get("MARKET_DIR") or None)
    ap.add_argument("--positions", default=None)
    ap.add_argument("--model-dir", default=os.environ.get("MODEL_DIR") or None,
                    help="champion store for continual learning (volume-persistent)")
    ap.add_argument("--refit", default=os.environ.get("REFIT") or None,
                    choices=[None, "auto", "full", "update"],
                    help="auto: warm-update champions daily, full refit on cadence")
    args = ap.parse_args()
    run_predict(args.m1, args.eod, args.out, args.config, args.max_tickers,
                market_dir=args.market, positions_csv=args.positions,
                model_dir=args.model_dir, refit=args.refit)
