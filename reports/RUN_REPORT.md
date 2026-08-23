# Final Run Report — EOD Swing & Position Trading System
**Blueprint:** v1.0.1, implemented in full · **Signals through:** close 2026-08-21
**All computation on RunPod** (CPU pods for data/stage1/stage3/predict, RTX-4090 for stage2).
Reports in this folder; browse them with the console in `app/` (see `app/README.md`).

## Verdict: ITERATE — the gates say do not trade this book
The kill floor is cleared (Rank IC 0.0299 ≥ 0.02, net Sharpe 0.556 ≥ 0.50),
so the signal is real. The advance-to-paper bar is not met: it needs Sharpe ≥ 0.80,
drawdown better than −15%, and it must beat both free baselines.

| Gate | Value | Threshold | Result |
|---|---|---|---|
| G-11 kill: Rank IC | 0.0299 | >= 0.0200 | PASS |
| G-11 kill: net Sharpe | 0.5557 | >= 0.5000 | PASS |
| G-11 advance: net Sharpe | 0.5557 | >= 0.8000 | FAIL |
| G-11 advance: max drawdown | -0.3269 | > -0.1500 | FAIL |
| G-08 beat SPY buy-and-hold | 0.5557 | > 0.6236 | FAIL |
| G-08 beat 12-1 momentum | 0.5557 | > 0.1227 | PASS |

## The deployed book (19y out-of-sample, net of 15 bps/leg + borrow)
Event engine with M5.2 barrier exits, regime overlay and vol targeting:
**Sharpe 0.556, annual return 6.1%, vol 10.9%,
max drawdown -32.7%**, 376,346 trades, average hold 17.3 sessions.
Growth of 1 → 4.18× from 2007. DSR 0.997 says the Sharpe is not selection luck.

Baselines it must beat: SPY buy-and-hold 0.624,
12-1 momentum 0.123. Alpha vs SPY
-3.30%/yr at beta 0.057.

CPCV(6,2) — 15 splits into 5 paths — median Sharpe 1.16,
worst-path drawdown -61.2%. A stability estimate only: it
trains across eras, so it is not live-replicable. The walk-forward 0.56 is the honest number.

## Members (out-of-sample Rank IC, stitched across folds)
- `lgbm_h20` +0.0260 (ICIR +0.247)
- `lgbm_h60` +0.0236 (ICIR +0.205)
- `lgbm_h5` +0.0229 (ICIR +0.185)
- `gru_h60` +0.0217 (ICIR +0.184)
- `gru_h20` +0.0193 (ICIR +0.171)
- `gru_h5` +0.0180 (ICIR +0.132)
- `cnn_I5R20` +0.0026 (ICIR +0.039)

The CNN fails the 0.02 admission floor (M10-03) and is excluded. The ensemble
(0.0299) beats its best single member
(0.0260) — the Stage-2 diversification gate passes.

## What was suggested, and what happened
376,346 barrier trades, 2007-01-10 → 2026-01-21:
- **Win rate 54.9%** — squarely in the blueprint's "52–55% is what
  winning looks like" band (G-12). Average net return **+0.93% per trade**.
- Exits: 64,808 profit-take (17.2%),
  51,503 stop (13.7%),
  259,518 time barrier (69.0%).
  Two-thirds of trades run to the 20-session vertical exit.
- Conviction inside the book is flat-to-noisy across rank deciles
  (0.56%..1.41%).
  Expected: the book only enters decile-10 names, so the ranking earns its keep at
  the *selection* step, not by fine-grading winners already in the book.

## Findings worth acting on
1. **Barrier widths degenerate on high-volatility names.** 39 of
   315 current suggestions price a barrier wider than ±40%
   (widest ±338%). thr = m·σ·√h is ±6.7σ, so a name at
   ~13% daily vol gets a ±90% barrier it can never touch — those trades are
   time-exit-only and their stop is not risk control. A width cap is an [IMPL]
   decision the spec leaves open; the console flags them meanwhile.
2. **Exit discipline is the biggest single win** — barrier exits are worth roughly
   +0.8 Sharpe over the Stage-1 fixed-tranche rotation (-0.27 → 0.56).
3. **The meta gate does not earn adoption** (M11-02): it cuts trades by
   69% but Sharpe falls to 0.07.
4. **Trials ledger must persist across pod runs** or DSR's N undercounts and the
   deflation flatters the result. Now written to the network volume.

## Next steps (all DSR-counted)
Optuna within spec ranges (barrier m/h, meta threshold, ensemble weights) ·
earnings-skip entries · per-name borrow from the iBorrowDesk table already fetched ·
merge the vol-scaled momentum sleeve · then the blueprint's own path: paper-trade
3–6 months measuring open-print slippage, which the console is built to record.

*Research tooling evaluated under its own go/no-go gates. Not financial advice.*
