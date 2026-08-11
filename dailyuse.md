# Daily use — data acquisition (EODHD + Nasdaq Data Link/Sharadar + Tiingo)

Downloads the data items the [Data Acquisition Specification — FINAL v1.2](Data%20Acquisition%20Specification%20%E2%80%94%20FINAL%20v1.2.md)
needs, stores them on a persistent RunPod network volume as JSON, and mirrors them back to the repo.
**One** set of scripts, **one** `.env`, **one** volume — pick the vendor at launch:

- `scripts/launch.sh all` → **the daily routine**: EODHD + Sharadar + Tiingo, one pod each
  (a vendor whose pod is still running is skipped, not doubled — safe to re-invoke)
- `scripts/launch.sh` → **EODHD** only (`fetch.py` → `data/`)
- `scripts/launch.sh nasdaq` → **Sharadar / Nasdaq Data Link** only (`fetch_nasdaq.py` → `data_nasdaq/`)
- `scripts/launch.sh tiingo` → **Tiingo** tertiary D-12 cross-check only (`fetch_tiingo.py` → `data_tiingo/`)

The other spec vendors (IBKR short-stock FTP, FinBERT, `exchange_calendars`, iBorrowDesk) are
separate pullers, out of scope here — see "Still to be coded" below.

> **TEST WINDOW (current):** every config is pinned to **1 year** (`from: 2025-07-28`) for the
> 500-stock trial run. Once validated, widen to 5 years by setting `from`/`market_from`/
> `eod_bulk.from` (tickers.json), `from`/`sf1_from` (sharadar.json), and `from` (tiingo.json) to
> `2021-07-28` — `news_from` stays late-2020+ (EODHD news API depth limit).

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

## Still to be coded (spec v1.2 items with no puller yet)

| Spec item | Vendor / source | Auth needed | Notes |
|---|---|---|---|
| **D-10** borrow fees/availability | IBKR public short-stock file (anonymous FTP `ftp3.interactivebrokers.com`, user `shortstock`) + iBorrowDesk JSON | none | forward-only — history accrues from day 1, so this collector is the most time-sensitive gap (G-05) |
| **D-11** trading calendar (source of truth) | `exchange_calendars` pip package | none | EODHD `exchanges` snapshot is only the cross-check |
| **D-16** FinBERT weights | Hugging Face `ProsusAI/finbert` | none (free) | one-time pull, pin revision hash |
| §4 parsing / landing zone | — | — | verbatim JSON → long-format Parquet, provenance columns, qlib bridge (module M1 proper) |
