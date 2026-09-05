# Research console — backend + frontend

A read-only view of the pipeline's final reports, plus the paper-trading book the
blueprint requires before real capital (BP15).

```
app/
  backend/    Node + Express on Vercel — serves the report bundle from MongoDB, owns the paper book
  frontend/   React + Vite on Render  — dashboard, suggestions, backtest explorer, paper trading
```

## Where the numbers come from

Nothing is recomputed in the app. `tools/build_reports.py` reads the artifacts the
RunPod jobs wrote (`derived/`) and emits `reports/latest/*.json`;
`tools/publish_mongo.py` copies those files verbatim into MongoDB Atlas (database
`InvestOpediaClaude`, collection `reports`, one document per section, plus a
`predictions` document per `as_of_close` as history). The API slices that. So a
number on screen always equals the number the pipeline produced — the API cannot
drift from it.

```
pods -> derived/ -> build_reports.py -> reports/latest/*.json -> publish_mongo.py -> MongoDB
                                                                                       |
                                                Render UI  <-- Vercel API  <-----------+
```

## Refresh after a pipeline run

`.claude/skills/daily-pipeline/scripts/mirror_reports.sh` (or `scripts/daily.sh`)
does all of this; by hand:

```bash
python3 tools/build_reports.py --src derived --out reports/latest
python3 tools/publish_mongo.py --bundle reports/latest      # needs MONGO_URI/DB_PASSWORD in .env
```

Until the publish step runs, the deployed console shows the previous run.

## Deploy

Live: UI https://investopediaclaudefe.onrender.com · API https://invest-opedia-claude-be.vercel.app/api/health

| piece | where | how | config |
|---|---|---|---|
| `backend/` | Vercel | import the `InvestOpediaClaudeBE` repo, preset *Other* | `MONGO_URI`, `DB_PASSWORD`, `CORS_ORIGIN` |
| `frontend/` | Render | static site from `InvestOpediaClaudeFE` (`render.yaml`), publish `dist` | `VITE_API_BASE` = the Vercel URL |

Atlas → Network Access must allow `0.0.0.0/0` (Vercel functions have no fixed IP).
The two deploy repos are `git subtree` mirrors of these directories — edit here,
commit, then `scripts/publish_repos.sh`. Per-directory READMEs have the details.

## Run locally (dev / tests)

```bash
cp app/backend/.env.example app/backend/.env      # Mongo creds; omit to fall back to reports/latest files
cd app/backend && npm install && npm start        # http://localhost:8787 (API + built UI if frontend/dist exists)
cd app/frontend && npm install && npm run dev     # http://localhost:5173, /api proxied to 8787
cd app/frontend && npm run build                  # or build once and let the API serve it
```

`npm test` in `app/backend` runs the paper-book regression suite against the file
store; `npm run test:mongo` smoke-tests the real database.

## Pages

| Page | What it answers |
|---|---|
| **Today** (landing) | What do I do at the next open? Dates itself to the next NYSE session — Friday evening and all weekend both point at Monday — and diffs the target book against what you actually hold, so a name reads BUY only if it is not already held, SELL if held but dropped from the target, HOLD otherwise. Also lists time-barrier exits that come due, and warns if the signals predate the last close. |
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
