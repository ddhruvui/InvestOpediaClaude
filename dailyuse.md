# Daily use — prediction stack (blueprint v1.0.1 implementation)

> **Data is not fetched from this repo.** The vendor pulls (EODHD, Sharadar, Tiingo, borrow,
> calendar, FinBERT, minute bars) and the `validate` → `build_m1` landing layer live in the
> separate **DataAcquistion** repo (`../DataAcquistion`, its own `scripts/daily.sh`,
> `dailyuse.md` and `daily-fetch` skill). The two repos share nothing but the RunPod network
> volume: that repo writes `data/`, `data_nasdaq/`, `data_tiingo/`, `data_borrow/`,
> `data_calendar/`, `data_finbert/`, `data/tickdata/` and `m1/`; this repo reads them
> (`src/data/m1.py` for the M1 tables, `src/data/build_market.py` for the raw `eod_bulk`
> day-files, `data/market/` for SPY, `data/news/` and `data_finbert/` for stage2). The per-ticker
> OHLCV files in `data/ohlcv/<T>.json` are never opened here: they reach the models only through
> the `m1/` tables `build_m1` builds from them. This repo writes ONLY under `results/InvestOpediaClaude/` (`m1x/`, `derived/`, `models/`,
> `ledger/`, `reports/`, its `_pod_logs/` and code bundle). Credentials for the volume + MongoDB:
> `runpod/.env` (copy `runpod/.env.example`; same volume id as the DataAcquistion `.env`).

## GPU fallback when EU-RO-1 has no CPU (automatic)

`launch_predict.sh`'s CPU jobs (`test`/`market`/`stage1`/`stage3`/`predict`/`publish`) try a
**CPU pod first** and, only on a capacity refusal, fall back to the cheapest available GPU
(A4500 first: 12 vCPU / 62 GB for ~$0.25/hr, vs 8 vCPU / 16 GB on CPU). Nothing about the
compute changes — the GPU is rented for the host slot only, and the pod still runs the CPU
image and CPU pip set. `stage2` keeps its own native GPU path. Only a genuine capacity refusal
triggers it; a bad token or malformed request still fails loudly.

    GPU_FALLBACK=0 scripts/launch_predict.sh market       # disable, CPU-only (will wait)
    RUNPOD_GPU_FALLBACK_TYPES='NVIDIA RTX A4500|NVIDIA A40' scripts/launch_predict.sh predict

The modelling/backtest/prediction system lives at repo root (`src/`, `configs/system.yaml`,
`tests/` — see [README.md](README.md)). **All processing runs on RunPod** against the
network volume the DataAcquistion repo fills; local execution is for unit tests and synthetic rehearsals only.

```sh
scripts/daily.sh                    # THE daily loop, one command: gate (volume current?)
                                    #   -> market -> predict (pod publishes to MongoDB)
                                    #   (SKIP_DATA_CHECK=1 / REFIT=full)

scripts/launch_predict.sh test      # T-01..T-15 suite on a CPU pod (validates pod env)
scripts/launch_predict.sh market    # eod_bulk -> m1x whole-market panel + top-1000
                                    #   survivorship-free universe (G-05); resumable
scripts/launch_predict.sh stage1    # features -> LGBM heads (purged WF) -> book -> gates
scripts/launch_predict.sh stage2    # + GRU + JKX CNN + FinBERT (GPU pod)
scripts/launch_predict.sh stage3    # meta gate + barrier-exit event book + CPCV(6,2)
                                    #   (reads SCORES_DIR, default
                                    #   /workspace/results/InvestOpediaClaude/derived/stage2)
scripts/launch_predict.sh predict   # latest-close scores -> target book -> suggestions
                                    #   (continual: warm-updates stored champions daily,
                                    #   full refit auto every 21 sessions — see below)

scripts/watch_jobs.sh stage3 predict   # 10-min watchdog: status, failure tails,
                                       # ONE auto-relaunch per job
```

## Incremental daily learning (the Monday-morning answer)

Nothing retrains from scratch daily. The `predict` job is **continual**: LGBM champions
persist on the volume under `/workspace/results/InvestOpediaClaude/models/` (`MODEL_DIR`), and each daily run

1. **decides the mode** — `update` if every head has a champion trained under the current
   `config_hash` and the last FULL fit is < `continual.full_refit_sessions` (21 ≈ monthly,
   = `val.retrain_cadence`) worth of *newly labeled* sessions old; else `full`;
2. in `update` mode loads only a `panel_tail_years` (5y) slice of the panel — year-parts
   before the tail are never even read — and **warm-continues** each champion with
   LightGBM `init_model` on the newest labeled year (purged against the valid year,
   `update_learning_rate` 0.02, ≤ `update_boost_rounds` extra trees);
3. **champion vs challenger**: both are scored on the SAME purged valid year (mean daily
   Rank IC); the challenger is adopted only if it wins. Learning accrues when the new
   data teaches something; a noise-day challenger is rejected and the champion stands;
4. logs every fit — adopted or rejected — to the G-09 trials ledger (DSR's N stays honest),
   and stamps the decision into `suggestions.json` under `"training"`.

`REFIT=full scripts/launch_predict.sh predict` forces a from-scratch fit (also automatic
after any `configs/system.yaml` change, feature-set change, or on the 21-session cadence).

**The whole loop is one command: `scripts/daily.sh`**, run once the DataAcquistion fetch has
reported the volume current (its `daily.sh` ends with `DONE`). It gates on `m1/_manifest.json`
being newer than the newest `eod_bulk` day-file (and under 36 h old), then sequences
market → predict (each watched to completion via `watch_jobs.sh`, one auto-relaunch).
The predict pod then publishes the console bundle from the volume straight to MongoDB,
and `daily.sh` ends by confirming `publish=0` in its log — nothing is mirrored to this
machine. `SKIP_DATA_CHECK=1` runs on whatever the volume holds. Stage 1/2/3 are the
research/backtest reports — they only need re-running when code or config changes,
or on the monthly cadence to refresh the G-11 gate verdict; they are deliberately
NOT part of `daily.sh`. (`overnight_orchestrator.sh` and `finalize_overnight.sh`
are one-offs from the initial build, not the daily loop.)

- Pods self-terminate with a confirmed DELETE; a restart marker prevents billing loops.
  `KEEP_POD=1` keeps a pod alive for inspection; `RUNPOD_VCPU=8` (16 GB) is required for
  stage1/stage3/predict (the 4 GB default OOMs); stage2 needs the GPU flavor (automatic).
- Outputs land on the volume under `results/InvestOpediaClaude/derived/<job>/` (reports,
  scores, target weights, suggestions). Fetch with (after `. scripts/_common.sh`):
  `aws s3 cp $S3FLAGS $RESULTS/derived/stage3/ ./results/InvestOpediaClaude/derived/stage3/ --recursive`
- Nothing is kept locally: `results/` is gitignored, and the G-09 append-only trials ledger
  feeding the Deflated Sharpe N lives only on the volume
  (`results/InvestOpediaClaude/ledger/trials.parquet`).
- Ordering: `market` must exist before stage1/predict (`USE_MARKET=1` default);
  stage3 needs a prior stage1 or stage2 scores directory; predict is independent of
  stage3 and can run daily once the DataAcquistion fetch + post have landed on the volume.
- Tape hygiene for the whole-market panel (bar sanity, vintage seams, V-spikes,
  level flips, tape breaks) is applied inside `build_panel()` — see
  `src/data/panel.py` and the memory note `eod-bulk-tape-hygiene`.

## Research console (reports + paper trading)

Reports live **in MongoDB only** (database `InvestOpediaClaude`): the predict pod builds
the bundle from the volume and publishes it at the end of every run, and the human-readable
book (`suggestions_latest.md`) is stored there as text too. Nothing is kept in this repo
or on this machine. View it on the deployed console (Render UI → Vercel API → MongoDB).

```sh
# re-publish "latest" from the volume without re-running the model (2-vCPU pod, < 1 min)
scripts/launch_predict.sh publish
# publish a named research era from here (staged in a temp dir, nothing kept)
scripts/refresh_console.sh stage3_h60 h60

# serve it locally (reads the same Mongo; app/backend/.env holds the credentials)
cd app/backend && npm install && npm start      # the real console is Render UI -> Vercel API -> MongoDB
cd app/frontend && npm run dev                  # hot-reload UI on :5173, proxies /api
cd app/backend && npm test                      # paper-book regression suite
```

Five pages: **Today** (the trade ticket — next-session orders, diffed against
what you hold, so new buys are distinguished from existing positions; it is the
landing page), **Dashboard** (G-11 verdict, gate table, equity curve, member Rank ICs,
CPCV spread, baselines), **Suggestions** (target book + barrier levels, push to
paper), **Backtest** (what was suggested vs what happened across 376k barrier
trades), **Paper trading** (BP15: record fills to measure open-print slippage,
PDT budget, kill switch, decay monitor). See [app/README.md](app/README.md).
