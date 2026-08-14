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

> **WINDOW (current, widened 2026-08-11):** prices/fundamentals `from = 2021-07-28` (**5 years**,
> 1,265 sessions), `news_from = 2020-12-01` (full EODHD depth), `market_from = 2000-01-01` (SPY is
> 1 credit per *call* regardless of window, so the 26-year history is free). Sharadar `sf1_from =
> 2021-07-28` gives ~20 quarters, which clears G-02's 12-quarter seasonal-diff SUE requirement for
> 493 of 503 tickers.
>
> **Widening again is now safe but was not always:** all three fetchers used to track only how far
> *forward* they had got, so moving a start date *backward* fetched nothing and the change silently
> did nothing. Each now detects it — EODHD news backfills the older gap, Sharadar compares against a
> recorded `_window.json` marker, and Tiingo treats a file as stale when its rows start later than
> the configured window. Note EODHD news depth is deeper than the spec's "~Dec 2020" claim for some
> names (KO reaches 2016-06-01), so there is more to take if you want it.

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
#    DAILY: run `all` AFTER ~21:00 UTC (17:00 ET) — EODHD's bulk day-file must not be pulled
#    mid-session or a partial file can be frozen (the fetcher skips existing day-files forever).
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
| `eod_bulk` | **resume** — newest-first, skip day-files already on the volume, `max_days_per_run` cap | date-partitioned; unadjusted OHLCV is immutable so old day-files never re-pull |
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
