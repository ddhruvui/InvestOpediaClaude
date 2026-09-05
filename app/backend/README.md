# InvestOpediaClaude — backend

Live at https://invest-opedia-claude-be.vercel.app (`/api/health`), serving the UI at
https://investopediaclaudefe.onrender.com.

Read-only API over the pipeline's published report bundle, plus the paper-trading
book (BP15). The numbers come from MongoDB, where `tools/publish_mongo.py` in the
main repo puts them after every pipeline run — nothing is recomputed here.

This directory is `app/backend/` of the main repo and is mirrored to its own GitHub
repo with `git subtree` (`scripts/publish_repos.sh` there). Edit it in the main repo.

## Data model (MongoDB, database `InvestOpediaClaude`)

| collection | key | holds |
|---|---|---|
| `reports` | `<bundle>/<section>` | the current bundle, one document per section (`summary`, `equity`, `suggestions`, `trades_summary`, `trades_sample`, `manifest`, `calendar`, `config`) |
| `predictions` | `<bundle>/<as_of_close>` | every published book, one per session — history the UI can browse |
| `paper_books` | `<bundle>` | the paper book this API writes |
| `publishes` | auto | one row per publish run (audit) |

## Environment

| var | required | meaning |
|---|---|---|
| `MONGO_URI` | yes | Atlas connection string; may contain the literal `<db_password>` placeholder |
| `DB_PASSWORD` | if placeholder used | substituted (URL-encoded) into `MONGO_URI` |
| `MONGO_DB` | no | database name, default `InvestOpediaClaude` |
| `REPORT_BUNDLE` | no | which bundle to serve, default `latest` |
| `CORS_ORIGIN` | no | comma-separated allowed origins (the Render UI URL); default any |

Without `MONGO_URI` the API falls back to `../../reports/<bundle>/*.json` and a JSON
paper book in `data/` — the dev/test mode only.

## Run locally

```sh
cp .env.example .env     # fill in MONGO_URI / DB_PASSWORD
npm install
npm start                # http://localhost:8787/api/health
npm test                 # paper-book + session tests (file store, no Mongo needed)
npm run test:mongo       # smoke test against the real database
```

## Deploy to Vercel

1. Import the `InvestOpediaClaudeBE` repo as a Vercel project (framework preset:
   **Other**; root directory: the repo root; no build command needed).
2. Add environment variables `MONGO_URI`, `DB_PASSWORD`, and `CORS_ORIGIN` (the
   Render UI origin, `https://investopediaclaudefe.onrender.com`).
3. In Atlas → Network Access, allow `0.0.0.0/0` (Vercel functions have no fixed IP).
4. Deploy. `vercel.json` rewrites every path to `api/index.js`, which exports the
   Express app, so `https://<project>.vercel.app/api/health` should answer.

## Routes

```
GET  /api/health                   source, bundle, published/built timestamps, verdict
GET  /api/today                    the session-aware trade ticket (BUY / SELL / HOLD)
GET  /api/summary | /equity | /suggestions | /config | /trades/summary
GET  /api/trades?limit&offset&exit&ticker&year&minRank&outcome&sortKey&sortDir
GET  /api/predictions[?limit]      published-book history (no payload)
GET  /api/predictions/:as_of       one day's full book
GET  /api/paper                    paper book + ops stats
POST /api/paper/open | /:id/fill | /:id/close | /settings | /reset
DELETE /api/paper/:id
```
