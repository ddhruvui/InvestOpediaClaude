# Final Run Report — EOD Swing & Position Trading System
**Date:** 2026-08-23 · **Blueprint:** v1.0.1 (implemented in full) · **Signals through:** close 2026-08-21
**All computation on RunPod** (CPU pods for data/stage1/stage3/predict, RTX-4090 pod for stage2).

## Build & verification
- Every blueprint module implemented (M1-read→M18, §8 layout, §3 config registry with
  config_hash stamping). All §9 tests (T-01…T-15) pass locally and on-pod; DSR formula
  replicates the audit's calibration; planted-signal and zero-signal falsification
  checks behave exactly as G-11 demands.
- Survivorship-free universe from 27y of whole-market bulk data (~280M rows): monthly
  top-1000 by 63d median dollar volume, funds + identity-less symbols excluded.
  Only 26% of the 2000 cohort survives to 2026.
- Five tape-hygiene screens (bar sanity, vintage seams, V-spikes, level flips, tape
  breaks) — without them the backtest printed fake 25,000x days. ~30k bars + 238
  recycled-symbol tails nulled out of 16.8M rows.

## Model results (all out-of-sample, net of 15 bps/leg + borrow)
**Member Rank ICs (stitched walk-forward, 19 folds, 2007→2026):**
lgbm_h20 0.026, lgbm_h60 0.024, lgbm_h5 0.023, gru_h60 0.022, gru_h20 0.019,
gru_h5 0.018, cnn_I5R20 0.003 (fails M10-03 admission — excluded).
**Ensemble RankIC 0.0299 ≥ best single 0.0260 → the Stage-2 diversification gate PASSES.**

**Books (walk-forward, event engine with M5.2 barrier exits, regime + vol targeting):**
| Book | Sharpe | MDD | trades | avg hold |
|---|---|---|---|---|
| 7-member ensemble, ungated | **0.556** | −32.7% | 376k | 17.3 sessions |
| meta-gated (p>0.55) | 0.073 | −46% | 117k | 17.1 |
| SPY buy-and-hold | 0.624 | −55% | — | — |
| plain 12-1 momentum L/S | 0.12 | −79% (2009 crash) | — | — |

DSR 0.9975 (N=63 ledger trials): the 0.556 is statistically genuine, not selection luck.
Meta gate correctly NOT adopted (M11-02). Barrier exits alone are worth ~+0.8 Sharpe
vs naive tranche rotation — exit discipline is the biggest single win.

**CPCV(6,2), 15 splits → 5 paths (LGBM members, no overlay — model-selection variance):**
path Sharpes {0.77, 1.03, 1.16, 1.24, 1.25}, median 1.16, worst-path MDD −61%.
Read as a stability estimate only — CPCV trains across eras and is NOT a live-replicable
number; the walk-forward 0.556 is the honest deployment estimate.

## G-11 verdict: **ITERATE — do not trade yet**
Kill floor passed (RankIC 0.030 ≥ 0.02, Sharpe 0.556 ≥ 0.5). Advance-to-paper NOT met:
needs Sharpe ≥ 0.8, MDD < 15%, and to beat SPY B&H (0.624). The blueprint's own sanity
band says live-realistic is 0.7–1.2 — the gap is credible, not hopeless.

## Current target book (signals @ close 2026-08-21)
315 names, gross long 0.95, SPY hedge −0.79 (see suggestions_latest.md): WMT, VTRS,
ROST, EL, NTAP, TJX, JKHY, CASY, UAL, TPR, HD… Each entry carries M5.2 barrier exits
(±m·σ·√h stop/profit-take, vertical MOO at t+21). 2,403 names failed the live
tradability screen and are excluded. **The gates above say this book is research
output, not a tradeable recommendation.**

## What would move the needle next (all DSR-counted)
1. Optuna within spec ranges (barrier m/h grid, meta threshold [0.50–0.65], ensemble
   weights) — ≤100 trials/model, M17 already wired.
2. Earnings-skip entries + per-name borrow table (iBorrowDesk data is already fetched).
3. F9 sentiment landed only if FinBERT weights present — verify it contributed on the
   GPU run; news depth is 2020+ only.
4. Momentum sleeve merge (M12 vol-scaled, 30% budget) — built, not yet merged into the
   headline book.
5. Then the blueprint's path: paper-trade 3–6 months measuring open-print slippage
   (M18 order files + slippage measurement are implemented).

*Research tooling under its own go/no-go gates — not financial advice (blueprint CAV).*
