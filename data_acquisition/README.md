# data_acquisition — EODHD ingestion (spec v1.2)

Replicates the InvestOpedia RunPod store → download → view → clean workflow, scoped to the
**EODHD** column of `../Data Acquisition Specification — FINAL v1.2.md`. A CPU pod runs
`src/fetch.py` against a persistent RunPod network volume (mounted at `/workspace`, exposed over an
S3 API), writing verbatim JSON per pull, then self-terminates. The scripts drive the volume from your
laptop; no data plane runs locally.

```
data_acquisition/
├── config/tickers.json     universe + which datasets + backfill windows
├── runpod/.env.example     EODHD token + RunPod account/S3 keys + volume id (copy to .env)
├── src/
│   ├── fetch.py            the fetcher — pure stdlib, one JSON file per (dataset, symbol)
│   └── bootstrap.sh        pod entrypoint: run fetch.py under an 8h watchdog, then self-terminate
└── scripts/
    ├── _common.sh          loads runpod/.env, sets S3 flags + bucket (sourced by the rest)
    ├── launch.sh           STORE:    upload code, create the pod (fire-and-forget)
    ├── download.sh         DOWNLOAD: mirror volume → repo-root ./data/ (skips code/)
    ├── storage_usage.sh    VIEW:     list volume contents + object count & size
    ├── clear_storage.sh    CLEAN:    wipe the volume (or just --logs)
    └── killpod.sh          safety net: terminate a pod that didn't self-terminate
```

## EODHD coverage vs spec D-items

Every EODHD row of the spec's §2 registry is mapped to a dataset here. Non-EODHD items
(D-04 Sharadar ACTIONS, D-05 Sharadar SF1 primary, D-10 IBKR borrow, D-11 `exchange_calendars`
source-of-truth, D-12 Sharadar SEP, D-16 FinBERT) belong to other vendors and are **not** built here.

| Spec item | EODHD endpoint | Dataset / output |
|---|---|---|
| **D-01** Daily prices → `raw_prices_eod` (per-ticker top-up) | `eod/{T}.US?period=d` | `eod` → `data/<T>.json` |
| **D-01** Daily prices → `raw_prices_eod` (**primary backfill**, survivorship-bias-free) | `eod-bulk-last-day/{EXCH}?date=` | `eod_bulk` → `data/eod_bulk/US/<DATE>.json` |
| **D-02** Splits → `corporate_actions(split)` | `splits/{T}.US` | `splits` → `data/splits/<T>.json` |
| **D-03** Cash dividends → `corporate_actions(div_cash)` | `div/{T}.US` | `dividends` → `data/dividends/<T>.json` |
| **D-05/06** PIT fundamentals + shares (EODHD = cross-check) | `fundamentals/{T}.US` | `fundamentals` → `data/fundamentals/<T>.json` |
| **D-07** Earnings history → `earnings_calendar` | `fundamentals/{T}.US` → `Earnings::History` | in the `fundamentals` object |
| **D-07** Earnings *upcoming* (Q-004 coverage, F8) | `calendar/earnings?from=&to=&symbols=` | `earnings_upcoming` → `data/earnings/upcoming.json` |
| **D-08** News → `news_headlines` | `news?s={T}.US` (paginated) | `news` → `data/news/<T>.json` |
| **D-09** Index prices SPY → `index_prices` | `eod/SPY.US` + `div/SPY.US` | `market` + `market_dividends` → `data/market/…` |
| **D-13** Entity master / delisted inventory (EODHD = secondary) | `exchange-symbol-list/US?delisted=1` | `symbol_lists` → `data/symbols/US.json` |
| **D-14** Analyst estimates → `analyst_estimates` | `fundamentals/{T}.US?filter=Earnings::Trend` | `estimates` → `data/estimates/<T>.json` (append-only) |
| **D-15** Historical S&P constituents → `index_constituents` | `fundamentals/GSPC.INDX` → `HistoricalTickerComponents` | `index_constituents` → `data/universe/GSPC.INDX.json` |
| **D-11** Trading calendar (EODHD = cross-check) | `exchange-details/US` | `exchanges` → `data/calendar/US.json` |

Explicitly **not** pulled (spec §2 "Confirmed NOT required"): insider transactions, VIX, technical
indicators, EODHD historical market-cap — none have a v1.0.1 consumer.

## D-01 bulk backfill (survivorship-bias-free)

The per-ticker `eod` job only covers the current `stocks` list, so it can't see names that were in the
index historically but have since delisted. The spec's D-01 **primary** backfill closes that: the
`eod_bulk` block loops `eod-bulk-last-day/US?date=YYYY-MM-DD` over trading days and stores one file per
day containing **every** ticker that traded — delisted included — giving the survivorship-bias-free
price history §7 requires.

```json
"eod_bulk": { "enabled": true, "exchange": "US", "from": "2000-01-01", "to": null, "max_days_per_run": 500 }
```

- **Cost:** ~100 credits/day-file; a full 2000→now backfill is ~650k credits (spec §4). `max_days_per_run`
  (default 500 = ~50k credits) bounds a single run so it stays under EODHD's 100k-credit/day cap.
- **Resumable:** the volume persists, so each run works **newest-first** and skips day-files already
  present — the cold backfill completes over ~13 daily runs; warm runs just add the latest day. Raise
  `max_days_per_run` (toward ~900) once the one-time per-ticker/news backfill is done and there's daily
  headroom. Set `"enabled": false` to pause it.
- **Adjusted close:** `adjusted_close` in a date-file is as-of-pull and may drift after a later split;
  that's by design — the canonical Q-002 factor comes from D-02/D-03, and the **unadjusted** OHLCV
  (D-01's source of truth) is immutable, so old files never need re-pulling.
- **Note:** `download.sh` mirrors every day-file, so a full backfill is thousands of small files under
  `data/eod_bulk/US/` — expected.

## Storage layout on the volume

```
code/            uploaded fetcher (fetch.py, bootstrap.sh, tickers.json) — skipped by download.sh
data/
├── <T>.json                    D-01 eod (per-ticker, current universe)
├── eod_bulk/US/<DATE>.json     D-01 whole-exchange bulk backfill (all tickers incl. delisted)
├── dividends/<T>.json          D-03
├── splits/<T>.json             D-02
├── fundamentals/<T>.json       D-05/06 + D-07 history
├── estimates/<T>.json          D-14 (append-only snapshots)
├── news/<T>.json               D-08
├── market/SPY.US.json          D-09 price
├── market/dividends/SPY.US.json D-09 dividends
├── universe/GSPC.INDX.json     D-15
├── calendar/US.json            D-11 cross-check
├── symbols/US.json             D-13
├── earnings/upcoming.json      D-07 forward
├── _run.json                   run manifest (vendor, provenance, per-job results)
└── logs/                       gated by STORE_LOGS (errors/crashes always logged)
```

See [../dailyuse.md](../dailyuse.md) for the command cheatsheet, incremental-run semantics, and the
local (no-pod) invocation.
