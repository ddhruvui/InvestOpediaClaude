# Daily use — EODHD acquisition

Downloads every **EODHD** data item the [Data Acquisition Specification — FINAL v1.2](Data%20Acquisition%20Specification%20%E2%80%94%20FINAL%20v1.2.md)
needs, stores it on a persistent RunPod network volume as JSON, and mirrors it back to the repo.
Non-EODHD vendors in the spec (Sharadar, the IBKR short-stock FTP file, FinBERT, `exchange_calendars`,
iBorrowDesk, Tiingo) are out of scope for this module — they are separate pullers.

One-time: `cp data_acquisition/runpod/.env.example data_acquisition/runpod/.env` and fill it in
(EODHD token + RunPod account/S3 keys + network-volume id). Edit the universe in
`data_acquisition/config/tickers.json`.

```sh
# 1. Fetch: uploads the fetcher, launches a CPU pod that downloads the EODHD set to the
#    volume, then self-terminates. Fire-and-forget.
data_acquisition/scripts/launch.sh

# 2. Download: pull every file except code/ into ./data/ at the repo root
data_acquisition/scripts/download.sh

# 3. View: list volume contents + total object count & size
data_acquisition/scripts/storage_usage.sh

# 4. Clear: wipe the volume entirely (asks to confirm; add -y to skip)
data_acquisition/scripts/clear_storage.sh
#    Logs only (leave data + code intact):
data_acquisition/scripts/clear_storage.sh --logs        # add -y to skip confirm

# Safety net: kill any pod that failed to self-terminate (normally never needed)
data_acquisition/scripts/killpod.sh
```

`config/tickers.json` drives every download. See [data_acquisition/README.md](data_acquisition/README.md)
for the full dataset → spec-D-item map and storage layout.

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
```
