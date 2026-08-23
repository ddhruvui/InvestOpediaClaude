# Research console — backend + frontend

A read-only view of the pipeline's final reports, plus the paper-trading book the
blueprint requires before real capital (BP15).

```
app/
  backend/    Node + Express — serves the report bundle, owns the paper book
  frontend/   React + Vite — dashboard, suggestions, backtest explorer, paper trading
```

## Run it

```bash
# 1. build the report bundle from pod artifacts (see below)
python3 tools/build_reports.py --src derived --out reports/latest

# 2. API (also serves the built UI on the same port if frontend/dist exists)
cd app/backend && npm install && npm start        # http://localhost:8787

# 3. UI in dev mode (hot reload, proxies /api to 8787)
cd app/frontend && npm install && npm run dev     # http://localhost:5173
# …or build once and let the API serve it:
cd app/frontend && npm run build
```

`npm test` in `app/backend` runs the paper-book regression suite.

## Where the numbers come from

Nothing is recomputed in the app. `tools/build_reports.py` reads the artifacts the
RunPod jobs wrote (`derived/`) and emits `reports/latest/*.json`; the API slices
that bundle. So a number on screen always equals the number the pipeline produced —
the API cannot drift from it.

To refresh after a pipeline run:

```bash
aws s3 cp $S3FLAGS s3://<volume>/derived/stage3/ derived/ --recursive
aws s3 cp $S3FLAGS s3://<volume>/derived/predict/suggestions.json derived/
python3 tools/build_reports.py --src derived --out reports/latest
```

## Pages

| Page | What it answers |
|---|---|
| **Dashboard** | Does this ship? G-11 verdict, the gate table, equity curve, member Rank ICs against the 0.02 admission floor, CPCV path spread, cost sensitivity, and the two free baselines it must beat. |
| **Suggestions** | What would I trade at the next open? The target book with each entry's triple-barrier levels, flagged where the barrier is too wide to ever trigger. One click pushes a name into the paper book. |
| **Backtest** | What was suggested, and what happened? 376k barrier trades: exit mix, win rate, return by conviction decile and holding length, and a filterable ledger. |
| **Paper trading** | The BP15 stage. Record fills against the official open to measure slippage (M18 → M15-03), watch the PDT budget and kill switch, and track the G-11 decay monitor. |

## Colour

Profit/loss uses market convention — green up, red down — which is the sanctioned
use of the status palette (the colour genuinely means good/bad). Green vs red
measures ΔE 4.1 under deuteranopia, far below the ≥8 separation target, so **hue is
never the only channel**: every profit/loss value also carries a sign, a ▲/▼ glyph,
or an explicit word ("Profit-take" / "Stop"), and diverging bars sit above or below
a zero baseline. Series identity (book vs SPY vs momentum) uses the validated
blue/orange/aqua categorical order, which clears all-pairs CVD separation in both
light and dark mode.
