# Daily use — data acquisition (EODHD + Nasdaq Data Link/Sharadar + Tiingo)

Downloads the data items the [Data Acquisition Specification — FINAL v1.2](Data%20Acquisition%20Specification%20%E2%80%94%20FINAL%20v1.2.md)
needs, stores them on a persistent RunPod network volume as JSON, and mirrors them back to the repo.
**One** set of scripts, **one** `.env`, **one** volume — pick the vendor at launch:

- `scripts/launch.sh all` → **the daily routine**: EODHD + Sharadar + Tiingo + borrow + calendar,
  one pod each (a vendor whose pod is still running is skipped, not doubled — safe to re-invoke)
- `scripts/launch.sh` → **EODHD** only (`fetch.py` → `data/`)
- `scripts/launch.sh nasdaq` → **Sharadar / Nasdaq Data Link** only (`fetch_nasdaq.py` → `data_nasdaq/`)
- `scripts/launch.sh tiingo` → **Tiingo** tertiary D-12 cross-check only (`fetch_tiingo.py` → `data_tiingo/`)
- `scripts/launch.sh borrow` → **D-10 borrow** (`fetch_borrow.py` → `data_borrow/`): IBKR public
  short-stock file + iBorrowDesk history. No key needed. **Never skip this one** — see below.
- `scripts/launch.sh calendar` → **D-11 NYSE sessions** (`fetch_calendar.py` → `data_calendar/`),
  the Q-001 source of truth incl. FUTURE sessions. Free, no key.
- `build_m1.py` → **§3/§4 parse + landing layer**: raw vendor JSON → the M1 tables as Parquet,
  plus the qlib bridge. Needs pandas+pyarrow, so it runs locally rather than on a stdlib-only pod:
  `OUT_DIR=./m1 EOD_DIR=./data … python3 data_acquisition/src/build_m1.py`.
  **This is what a model reads.** It is also where every consumption rule is enforced rather than
  documented — session grid, raw-close provenance, quarantine, vintages, permaticker, split-vs-spinoff.
- `scripts/launch.sh post` → **the closing stage, and the one that keeps models in step with the
  data.** Waits for vendor manifests NEWER THAN ITS OWN LAUNCH (stamped via `POST_LAUNCHED_AT`;
  fetchers fire moments earlier, so a newer manifest proves a same-batch fetch completed), then
  runs `validate` and `build_m1` in order in one pod. Fire it at the same time as `all` — it
  self-sequences. (The old today's-UTC-date gate failed both ways around midnight UTC — a prior
  run ending after 00:00 UTC pre-satisfied the next evening's gate so m1 built on the prior close,
  and a post launched after 00:00 UTC waited out its whole timeout. Observed 2026-08-24/25.)

      scripts/launch.sh all && scripts/launch.sh post

  Without it the M1 Parquet tables silently stay at yesterday's build while the raw volume moves on.
- `scripts/launch.sh validate` → **D-12 / M1-04 cross-vendor check + Q-004 + repair**
  (`validate.py` → `data_quality/`). Reads the volume only — no API calls, no credits.
  **Run it AFTER the nightly `all` finishes**, not as part of it: `all` launches pods in parallel and
  this job consumes the others' output. It is idempotent, so re-running is always safe.

- `scripts/launch.sh finbert` → **D-16 FinBERT weights** (`fetch_finbert.py` → `data_finbert/`) at a
  PINNED commit sha, not `main`: HuggingFace `main` moves, and two revisions give two different F9
  scores, silently breaking G-10 reproducibility. Free, idempotent (size+sha checked).

Every spec vendor now has a puller.

> **The borrow job is the one with a deadline.** Spec G-05: borrow history cannot be bought
> retroactively. iBorrowDesk gives a **rolling ~1 year** of daily history, which the fetcher merges
> append-only, so a missed day is recoverable for about 12 months and permanently lost after that.
> The IBKR snapshot half is *immediately* unrecoverable — it is a live indicative file with no
> history at all.

> **WINDOW (current, 2026-08-14).** Prices and corporate actions run to the spec's floor; fundamentals
> start earlier still, on purpose.
>
> | feed | start | why |
> |---|---|---|
> | EODHD `eod` + `eod_bulk` | `2000-01-01` | spec §2 D-01 |
> | Sharadar SEP / ACTIONS | `2000-01-01` | matches D-01 |
> | Sharadar SF1 (`sf1_from`) | **`1998-01-01`** | F7 asset growth needs 5 quarters and G-02's SUE needs 12, so fundamentals must start BEFORE the price window or those features only appear ~3 years in |
> | EODHD news | `2020-12-01` | vendor depth (reaches ~2016 for some names) |
> | SPY (`market_from`) | `2000-01-01` | 1 credit per *call*, so depth is free |
>
> Sharadar is on the **full-history bundle** (upgraded 2026-08-14): SF1 reaches 1992-12-31 and ACTIONS
> 1997-12-31 with 19,231 delistings. Before the upgrade the retail tier capped SF1 at ~5 years and
> `years=10` returned 403 — if fundamentals history ever truncates again, check the subscription first.
>
> **Widening is safe to repeat.** All three fetchers used to track only how far *forward* they had got,
> so moving a start date *backward* silently did nothing. Each now detects it: EODHD news backfills the
> older gap, Sharadar compares a recorded `_window.json` marker, Tiingo checks the stored rows' start date.
>
> **`eod_bulk` to 2000 is credit-bound, not disk-bound:** ~5,400 sessions x 100 credits ≈ 5.4 nightly
> runs at the 100k/day cap, and ~21.5 GiB (day-files were 2 MiB in 2000, not today's 6.6 MiB).

## GPU fallback when EU-RO-1 has no CPU (automatic)

Every CPU job — all six fetchers, `validate`/`m1`/`post`, and `launch_predict.sh`'s
`test`/`market`/`stage1`/`stage3`/`predict` — now tries a **CPU pod first** and, only on a
capacity refusal, falls back to the **cheapest available GPU**. The network volume pins us to
one datacenter, and EU-RO-1 CPU capacity has gone to zero for hours at a time (2026-08-24, -26,
-27), which is enough to miss the open. GPU hosts are a separate pool and are usually free.

Nothing about the compute changes: these jobs are pandas/LightGBM and never touch CUDA. The
GPU is rented purely for the host slot, and the pod still runs the **CPU image and CPU pip
set** — not the PyTorch image. `stage2` is unaffected; it keeps its own native GPU path.

A GPU pod is also *far* better resourced than any CPU flavor, so the fallback incidentally
removes the OOM risk (measured in EU-RO-1, 1 GPU + volume, 2026-08-27):

| host | $/hr | vCPU | RAM |
|---|---|---|---|
| CPU 4 vCPU (m1/post/validate floor) | ~0.10 | 4 | 8 GB |
| CPU 8 vCPU (market/predict) | ~0.20 | 8 | 16 GB |
| **GPU RTX A4500** (first choice) | **0.25** | 12 | **62 GB** |
| GPU RTX 4000 Ada | 0.28 | 9 | 50 GB |
| GPU RTX 4090 | 0.74 | 16 | 61 GB |

These jobs run for minutes, so the delta is cents. The launcher prints which host class took
the job (`placed on: GPU NVIDIA RTX A4500`).

    GPU_FALLBACK=0 data_acquisition/scripts/launch.sh all    # disable, CPU-only (will wait)
    RUNPOD_GPU_FALLBACK_TYPES='NVIDIA RTX A4500|NVIDIA A40' scripts/launch_predict.sh predict

Only a genuine capacity refusal triggers it — a bad token or malformed request still fails
loudly instead of quietly costing GPU money.

One-time: `cp data_acquisition/runpod/.env.example data_acquisition/runpod/.env` and fill it in
(RunPod account/S3 keys + network-volume id, plus the token for whichever vendor you launch —
`EODHD_API_TOKEN`, `SHARADAR_API_KEY`, and/or `TIINGO_API_TOKEN`). Edit the universe in
`data_acquisition/config/tickers.json` (EODHD), `config/sharadar.json` (Sharadar), or
`config/tiingo.json` (Tiingo).

> **Where to get `SHARADAR_API_KEY`:** individual users subscribe to the **Core US Equities Bundle**
> (Non-Professional tier) at <https://sharadar.com/subscribe> and copy the key from their sharadar.com
> account. The fetcher calls `https://api.sharadar.com/v1.0/data/<endpoint>` with it. (`data.nasdaq.com`
> is institutional-only — a sharadar.com key is anonymous there and gets rate-limited.)

```sh
# 1. Fetch: upload the vendor's fetcher+config, launch a CPU pod per vendor that downloads to the
#    volume, then self-terminates. Fire-and-forget.
#    DAILY: run `all` at ~22:00 UTC (18:00 ET). Not earlier than 21:00 UTC — EODHD publishes
#    the bulk day-file ~23:30 UTC and the fetch reaches the bulk job ~80 min in. The fetcher
#    re-pulls the trailing few sessions nightly (EODHD mutates recent day-files; the same-night
#    file runs ~12% light on late fund-NAV series), so an early pull self-heals the NEXT night —
#    but tonight's book prices against tonight's pull, so launch late anyway.
data_acquisition/scripts/launch.sh all        # DAILY ROUTINE: EODHD + Sharadar + Tiingo (one pod
                                              # each; already-running vendors are skipped, not doubled)
data_acquisition/scripts/launch.sh            # EODHD only (default)
data_acquisition/scripts/launch.sh nasdaq     # Sharadar / Nasdaq Data Link only
data_acquisition/scripts/launch.sh tiingo     # Tiingo only (cold pass ~5 h paced; warm daily runs
                                              # near-free — skip_fresh_days skips files < 5 days old)

# 2. Download: mirror the volume into the repo root (data/ EODHD, data_nasdaq/ Sharadar, data_tiingo/ Tiingo)
data_acquisition/scripts/download.sh

# 3. View: list volume contents + total object count & size
data_acquisition/scripts/storage_usage.sh

# 4. Clear: wipe the volume entirely (asks to confirm; add -y to skip)
data_acquisition/scripts/clear_storage.sh
#    Logs only (leave data + code intact):
data_acquisition/scripts/clear_storage.sh --logs        # add -y to skip confirm

# Safety net: kill any investopediaclaude-* pod that failed to self-terminate (normally never needed)
data_acquisition/scripts/killpod.sh
```

`config/tickers.json` (EODHD), `config/sharadar.json` (Nasdaq) and `config/tiingo.json` (Tiingo)
drive each download. See [data_acquisition/README.md](data_acquisition/README.md) for the full
per-vendor dataset → spec-D-item map, the Sharadar/Tiingo API mechanics, and the storage layout.

## Datasets (all EODHD)

**Per-equity** — `"datasets"` list applied to each `"stocks"` entry (default `["eod"]`):

| dataset | output on volume | spec item | notes |
|---|---|---|---|
| `eod` | `data/<TICKER>.json` | D-01 | OHLC + adjusted_close + volume (close unadjusted; factor = adjusted_close/close) |
| `dividends` | `data/dividends/<TICKER>.json` | D-03 | ex-date cash dividends |
| `splits` | `data/splits/<TICKER>.json` | D-02 | split ratios |
| `fundamentals` | `data/fundamentals/<TICKER>.json` | D-05/06 + D-07 | full lossless object (Highlights, SharesStats, Earnings.History/Trend, Sector) |
| `estimates` | `data/estimates/<TICKER>.json` | D-14 | Earnings::Trend snapshots — **append-only**, one dated row per pull day |
| `news` | `data/news/<TICKER>.json` | D-08 | timestamped articles; HEAVY — own `news_from` window (~Dec-2020 onward) |

**Market / index / exchange level** — separate config keys:

| config key | example | output on volume | spec item |
|---|---|---|---|
| `market` | `["SPY.US"]` | `data/market/<SYMBOL>.json` | D-09 SPY daily level (index_prices) |
| `market_dividends` | `["SPY.US"]` | `data/market/dividends/<SYMBOL>.json` | D-09 SPY dividends (total-return build) |
| `index_constituents` | `["GSPC.INDX"]` | `data/universe/<INDEX>.json` | D-15 survivorship-free membership |
| `exchanges` | `["US"]` | `data/calendar/<CODE>.json` | D-11 EODHD holiday cross-check |
| `symbol_lists` | `["US"]` | `data/symbols/<CODE>.json` | D-13 full inventory incl. delisted |
| `earnings_upcoming` | `true` | `data/earnings/upcoming.json` | D-07 forward earnings calendar |
| `eod_bulk` | `{enabled, from, max_days_per_run}` | `data/eod_bulk/US/<DATE>.json` | **D-01 primary backfill** — whole exchange per day, ALL tickers incl. delisted (survivorship-bias-free) |

`eod_bulk` is the spec's survivorship-bias-free price backfill: one file per trading day holding every
ticker (delisted included). It's a big one-time credit spend (~650k credits for 2000→now at ~100/day-file),
so it **resumes newest-first across runs** — bounded by `max_days_per_run` (default 500 ≈ 50k credits/run)
to stay under EODHD's 100k/day cap. Each launch skips day-files already on the volume; the cold backfill
finishes over ~13 daily runs, warm runs just add the latest day. See [data_acquisition/README.md](data_acquisition/README.md).

The default config pulls the complete EODHD set. `news` is the one heavy feed, so it has its own
`"news_from"` start date, independent of the price/fundamentals window.

## Incremental runs (`"incremental": true`, default)

The network volume **persists `data/` between launches**, so a re-launch only adds what's new:

| dataset | on a warm volume | why |
|---|---|---|
| `news` | **incremental** — fetch only rows dated ≥ the latest stored, merge & dedup | append-only; this is where the savings are |
| `estimates` | **incremental** — append one dated Earnings::Trend snapshot per pull day | D-14 immutable PIT history accrues forward |
| `eod_bulk` | **resume + trailing re-pull** — newest-first, skip day-files already on the volume except the trailing few sessions, which re-pull every night; `max_days_per_run` cap | EODHD mutates recent day-files (late fund-NAV prints, corporate-action rewrites — see vendor-facts table), so the tail must refresh; deep history is left as stored |
| `eod`, `dividends`, `splits`, `market`, `market_dividends` | **full refetch** (tiny) | EODHD rewrites `adjusted_close` retroactively after a split/dividend |
| `fundamentals`, `index_constituents`, `exchanges`, `symbol_lists`, `earnings_upcoming` | **full refetch** (snapshots) | point-in-time objects, replaced whole |

First launch on an empty volume = full backfill; every launch after = delta only. The run log shows
`(+N) [incr≥DATE]` per job. Set `"incremental": false` to force a full refetch. Don't run
`clear_storage.sh` between runs or you lose the warm state and re-backfill from scratch.

## Tiingo (`launch.sh tiingo`) — tertiary D-12 cross-check

| dataset / key | output on volume | spec item | notes |
|---|---|---|---|
| `prices` | `data_tiingo/<TICKER>.json` | D-12 (+D-01/02/03 cross) | unadj OHLCV + adj OHLCV + divCash + splitFactor per row |
| `metadata` | `data_tiingo/metadata/<TICKER>.json` | D-13 cross | name, exchange, coverage start/end (off in test config) |
| `news` | `data_tiingo/news/<TICKER>.json` | G-04 (paid add-on) | append-only incremental; OFF by default |
| `market` | `data_tiingo/market/SPY.json` | D-09 cross | fetched FIRST so the budget never starves it |
| `symbol_list` | `data_tiingo/symbols/supported_tickers.json` | D-13 cross | static CDN zip → JSON; no token/budget cost |

Free tier ≈ **50 req/hr, 1,000 req/day, 500 unique symbols/month per account** — and we run **two
accounts** (`TIINGO_API_TOKEN` + `TIINGO_API_TOKEN2` in `runpod/.env`): the fetcher pins the first
half of `stocks` to token 1 and the second half to token 2 (positional split, sticky within a month
— the unique-symbol cap counts per account) and interleaves the halves so each token paces its own
50 req/hr window (`min_request_interval_sec: 72` is per token → ~100 req/hr combined). 252 + 251
symbols + SPY on token 1 = both accounts under the 500/mo cap, and the whole universe finishes in
one ~5 h launch (`max_requests_per_run: 700`, inside the 8 h watchdog). Jobs past the budget log
`DEFER` (non-fatal, exit 0); re-launch and `skip_fresh_days: 5` resumes where it left off.

## Logging

`data/_run.json` (run manifest, per-(dataset,symbol) results + provenance) is always written.
`data/logs/` is **env-controlled** via `STORE_LOGS` (set in `runpod/.env`):

| `STORE_LOGS` | success | failure | crash |
|---|---|---|---|
| `false` (default) | no log | `logs/error-<ts>.log` | `logs/crash-<ts>.log` |
| `true` | `logs/run-<ts>.log` | `logs/error-<ts>.log` | `logs/crash-<ts>.log` |

Errors and crashes are **always** logged regardless of the flag; only the successful-run log is gated.

## Run the fetcher locally (no pod)

```sh
DATA_DIR=./data CONFIG_PATH=data_acquisition/config/tickers.json \
  EODHD_API_TOKEN=... STORE_LOGS=true python3 data_acquisition/src/fetch.py
DATA_DIR=./data_nasdaq CONFIG_PATH=data_acquisition/config/sharadar.json \
  SHARADAR_API_KEY=... STORE_LOGS=true python3 data_acquisition/src/fetch_nasdaq.py
DATA_DIR=./data_tiingo CONFIG_PATH=data_acquisition/config/tiingo.json \
  TIINGO_API_TOKEN=... STORE_LOGS=true python3 data_acquisition/src/fetch_tiingo.py
```

## Vendor facts the spec gets wrong (measured live 2026-08-11)

These were verified against the live APIs, not inferred. Each one broke, or would have broken, a run.

| Spec says | Actually | Consequence if you trust the spec |
|---|---|---|
| D-10 via `ftp3.interactivebrokers.com` | **ftp3 times out**; `ftp2.interactivebrokers.com` serves the same `usa.txt` anonymously | D-10 never collects |
| iBorrowDesk = "partial" history | Alive and good for a **rolling ~1 y daily** history — but only on the **`www.`** host **with a browser User-Agent** (apex host returns an empty reply; programmatic UAs get 403) | G-05 is softer than written: ~1 y of borrow history is backfillable on day 1 |
| Sharadar via `data.nasdaq.com` datatables | Retail keys are served by **`api.sharadar.com`**, with hard caps the spec never mentions: **30 tickers AND 200 chars** per `ticker` param, **100,000 rows** per response, `offset` paging, and a `years=N` bulk parameter | 400s on every batched call; silent truncation past 100k rows |
| Sharadar rate-limit headers | `x-ratelimit-*`, and `x-ratelimit-reset` is a **UNIX timestamp**, not a delay; there is also a separate weighted budget (a full-table call costs 100 of 25,000) | pacing is dead code; naively "fixing" the header name sleeps the pod for ~56 years |
| D-01 `close` is unadjusted | **EODHD rewrites `close` retroactively** after splits/spinoffs, and `eod-bulk-last-day` day-files are **mutable** — the same date pulled twice can differ (CMCSA 2025-07-28: 33.53 stored → 31.4246 live) | Q-002 factors are wrong for affected names; "immutable day-file" resume freezes a mix of vintages. Sharadar `closeunadj` and Tiingo `close` are the reliable raw prints |
| §8-6 `unadjustedValue` presence | Present on **100%** of dividend rows, alongside payment/record/declaration dates | settled — no fallback needed |

## Still to be coded (spec v1.2 items with no puller yet)

| Spec item | Vendor / source | Auth needed | Notes |
|---|---|---|---|
(D-16 FinBERT, the §3/§4 landing layer, the D-12 job and whole-market D-02/D-03 are all built now —
`fetch_finbert.py`, `build_m1.py`, `validate.py` and the `eod_bulk_actions` block respectively.)
| **D-02/D-03 bulk** | EODHD `eod-bulk-last-day?type=splits\|dividends` | EODHD | per-ticker pulls cover only the 503 configured names, while `eod_bulk` covers ~45k — corporate actions are not survivorship-free |

---

# Daily use — prediction stack (blueprint v1.0.1 implementation)

The modelling/backtest/prediction system lives at repo root (`src/`, `configs/system.yaml`,
`tests/` — see [README.md](README.md)). **All processing runs on RunPod** against the same
network volume as the fetchers; local execution is for unit tests and synthetic rehearsals only.

```sh
scripts/daily.sh                    # THE daily loop, one command: fetch -> post ->
                                    #   market -> predict -> mirror -> reports/latest
                                    #   (SKIP_FETCH=1 / REFIT=full / FULL_MIRROR=1)

scripts/launch_predict.sh test      # T-01..T-15 suite on a CPU pod (validates pod env)
scripts/launch_predict.sh market    # eod_bulk -> m1x whole-market panel + top-1000
                                    #   survivorship-free universe (G-05); resumable
scripts/launch_predict.sh stage1    # features -> LGBM heads (purged WF) -> book -> gates
scripts/launch_predict.sh stage2    # + GRU + JKX CNN + FinBERT (GPU pod)
scripts/launch_predict.sh stage3    # meta gate + barrier-exit event book + CPCV(6,2)
                                    #   (reads SCORES_DIR, default /workspace/derived/stage2)
scripts/launch_predict.sh predict   # latest-close scores -> target book -> suggestions
                                    #   (continual: warm-updates stored champions daily,
                                    #   full refit auto every 21 sessions — see below)

scripts/watch_jobs.sh stage3 predict   # 10-min watchdog: status, failure tails,
                                       # ONE auto-relaunch per job
```

## Incremental daily learning (the Monday-morning answer)

Nothing retrains from scratch daily. The `predict` job is **continual**: LGBM champions
persist on the volume under `/workspace/models/` (`MODEL_DIR`), and each daily run

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

**The whole loop is one command: `scripts/daily.sh`** (start ~22:00 UTC / 18:00 ET; EODHD's
bulk day-file lands ~23:30 UTC and the fetch should finish before midnight UTC). It
sequences fetch → post (waits for the pod AND verifies m1 was rebuilt *today*) →
market → predict (each watched to completion via `watch_jobs.sh`, one auto-relaunch)
→ mirrors `suggestions.json`/`.md` + stage reports off the volume → rebuilds
`reports/latest`. `SKIP_FETCH=1` when data is already in; `FULL_MIRROR=1` after a
stage3 rerun to re-pull the equity/trades parquets. Stage 1/2/3 are the
research/backtest reports — they only need re-running when code or config changes,
or on the monthly cadence to refresh the G-11 gate verdict; they are deliberately
NOT part of `daily.sh`. (`overnight_orchestrator.sh` and `finalize_overnight.sh`
are one-offs from the initial build, not the daily loop.)

- Pods self-terminate with a confirmed DELETE; a restart marker prevents billing loops.
  `KEEP_POD=1` keeps a pod alive for inspection; `RUNPOD_VCPU=8` (16 GB) is required for
  stage1/stage3/predict (the 4 GB default OOMs); stage2 needs the GPU flavor (automatic).
- Outputs land on the volume under `derived/<job>/` (reports, scores, target weights,
  suggestions). Fetch with:
  `aws s3 cp $S3FLAGS s3://<volume>/derived/stage3/ ./derived_stage3/ --recursive`
- `derived_*/` downloads are disposable and gitignored; keep only
  `artifacts/reports/*.json|md` (small, reviewable) and `ledger/trials.parquet`
  (G-09 append-only trials ledger feeding the Deflated Sharpe N).
- Ordering: `market` must exist before stage1/predict (`USE_MARKET=1` default);
  stage3 needs a prior stage1 or stage2 scores directory; predict is independent of
  stage3 and can run daily after `launch.sh all` + `post`.
- Tape hygiene for the whole-market panel (bar sanity, vintage seams, V-spikes,
  level flips, tape breaks) is applied inside `build_panel()` — see
  `src/data/panel.py` and the memory note `eod-bulk-tape-hygiene`.

## Research console (reports + paper trading)

Final reports live in **`reports/`** — `RUN_REPORT.md` and `suggestions_latest.md`
are the human-readable finals, `reports/latest/*.json` is the machine bundle the
app serves, `reports/raw/` keeps the pod stage-reports for provenance.

```sh
# refresh the bundle after a pipeline run (pods write to the volume; this only reads)
aws s3 cp $S3FLAGS s3://$RUNPOD_VOLUME_ID/derived/stage3/ derived/ --recursive
aws s3 cp $S3FLAGS s3://$RUNPOD_VOLUME_ID/derived/predict/suggestions.json derived/
python3 tools/build_reports.py --src derived --out reports/latest

# serve it
cd app/backend && npm install && npm start      # http://localhost:8787 (API + built UI)
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
