# data_acquisition — multi-vendor ingestion (spec v1.2)

Replicates the InvestOpedia RunPod store → download → view → clean workflow for the
`../Data Acquisition Specification — FINAL v1.2.md` external-data surface. **One** set of scripts,
**one** `.env`, **one** network volume; you pick the vendor at launch. A CPU pod runs the chosen
fetcher against a persistent RunPod network volume (mounted at `/workspace`, exposed over an S3 API),
writing verbatim JSON per pull, then self-terminates. All three fetchers give first-pass failures one
**end-of-run retry sweep** (after a 60s pause; `RETRY_SWEEP_DELAY` env) so a transient vendor 5xx blip
heals within the run — healed jobs carry `"retried": true` in `_run.json`. The scripts drive the
volume from your laptop; no data plane runs locally.

```
data_acquisition/
├── config/
│   ├── tickers.json        EODHD    universe + which datasets + backfill windows
│   ├── sharadar.json       Nasdaq   universe + which tables + windows + ticker normalization
│   └── tiingo.json         Tiingo   universe + datasets + free-tier pacing/budget
├── runpod/.env.example     EODHD + Nasdaq + Tiingo tokens + RunPod account/S3 keys + volume id (copy to .env)
├── src/
│   ├── fetch.py            EODHD fetcher   (scripts/launch.sh          -> data/)
│   ├── fetch_nasdaq.py     Sharadar fetcher (scripts/launch.sh nasdaq  -> data_nasdaq/)
│   ├── fetch_tiingo.py     Tiingo fetcher  (scripts/launch.sh tiingo   -> data_tiingo/)
│   └── bootstrap.sh        pod entrypoint: run $FETCH_SCRIPT under an 8h watchdog, then self-terminate
└── scripts/                shared across vendors — download/clear/storage_usage/killpod are vendor-agnostic
    ├── _common.sh          loads runpod/.env, sets S3 flags + bucket (sourced by the rest)
    ├── launch.sh [vendor]   STORE:    upload the vendor's fetcher+config, create the pod(s)
    │                                  (default: eodhd; `all` = eodhd+nasdaq+tiingo — the daily routine)
    ├── download.sh         DOWNLOAD: mirror volume → repo root (data/ + data_nasdaq/ + data_tiingo/, skips code/)
    ├── storage_usage.sh    VIEW:     list volume contents + object count & size
    ├── clear_storage.sh    CLEAN:    wipe the volume (or just --logs)
    └── killpod.sh          safety net: terminate any investopediaclaude-* pod that didn't self-terminate
```

**Vendor selection:** `scripts/launch.sh` runs EODHD (`fetch.py` → `data/`); `scripts/launch.sh nasdaq`
runs Sharadar (`fetch_nasdaq.py` → `data_nasdaq/`); `scripts/launch.sh tiingo` runs Tiingo
(`fetch_tiingo.py` → `data_tiingo/`); **`scripts/launch.sh all` runs EODHD + Sharadar + Tiingo (one pod
each) — use this for the daily run so no vendor gets skipped** (Tiingo's `skip_fresh_days` makes its
warm daily runs near-free). Per vendor, `launch.sh` uploads that vendor's fetcher + config to `code/`
and passes that vendor's token; the vendors share the volume without colliding (distinct top-level
namespaces). Each launch is fire-and-forget; `DRY_RUN=1` previews without uploading or creating pods.
A vendor whose pod is already running is skipped, not doubled (idempotent re-invoke). If one vendor's
launch fails, the others still launch and the script exits nonzero naming the failure.

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

---

# Sharadar (`scripts/launch.sh nasdaq`)

The spec (§1) makes Sharadar **mandatory** for PIT fundamentals (`data.vendors: sharadar+eodhd`).
`src/fetch_nasdaq.py` pulls every Sharadar row of the §2 registry via the **retail API at
`api.sharadar.com`**, writing to the `data_nasdaq/` namespace on the same volume. The other spec
vendors (IBKR borrow D-10, `exchange_calendars` D-11, FinBERT D-16) belong elsewhere and aren't here.

> **Retail vs institutional:** individual subscribers buy the Core US Equities Bundle at
> <https://sharadar.com/subscribe> and get an **`api.sharadar.com`** key. `data.nasdaq.com` is
> institutional-only now — a sharadar.com key is *anonymous* there and gets IP-throttled (`QELx06`).
> This fetcher targets the retail host; the two APIs share Sharadar's schema + filter operators but
> differ in host, endpoint names, response shape, and paging.

| Spec item | Table | api endpoint | Filters | Output |
|---|---|---|---|---|
| **D-12** second-vendor price cross-check (M1-04) | `SEP` | `stocks` | `ticker`, `date.gte` / `lastupdated.gte` | `data_nasdaq/SEP/<T>.json` |
| **D-05/06** PIT fundamentals + shares (**primary, mandatory**) | `SF1` | `fundamentals` | `ticker`, `dimension=ARQ`, `calendardate.gte` / `lastupdated.gte` | `data_nasdaq/SF1/<T>.json` |
| **D-04** splits, divs, spinoffs, delistings, ticker changes | `ACTIONS` | `actions` | `ticker`, `date.gte` | `data_nasdaq/ACTIONS/<T>.json` |
| **D-13** entity master / permanent id | `TICKERS` | `tickers` | whole table | `data_nasdaq/TICKERS/SHARADAR.json` |
| **D-15** historical S&P 500 constituents | `SP500` | `sp500` | whole table | `data_nasdaq/SP500/SHARADAR.json` |

Why Sharadar and not EODHD here: SF1 `dimension=ARQ` preserves *as-reported, filing-dated* originals
so features never see restatements (M1-01/T-11 — EODHD's single mutable record can't satisfy this,
§6 G-06); ACTIONS carries spinoff/stock-dividend/delisting-reason events EODHD has no feed for;
SEP is the independent second price vendor for the M1-04 cross-check; TICKERS' `permaticker` is the
M1 primary entity key that stitches symbol changes together. (SF1's SEC-filing date is the `date`
column on this API — the datatables API calls it `datekey`; M2-03's +1-session lag keys off it.)

**Where to get `SHARADAR_API_KEY`:** subscribe to the **Core US Equities Bundle** (Non-Professional
tier) at <https://sharadar.com/subscribe>, then copy the key from your sharadar.com account. Put it in
`runpod/.env` (see `.env.example`). A wrong/unentitled key returns **403** from `api.sharadar.com`.

**API mechanics** (`fetch_nasdaq.py`): `GET https://api.sharadar.com/v1.0/data/<endpoint>?api_key=…&format=json&limit=…`;
response `{"count":N, "data":[row-dicts]}` (rows already keyed — no columns/zip). **No cursor** — a
single high-`limit` call returns every matching row (TICKERS ≈ 25k rows in one call; a `count == limit`
result logs a truncation WARN). The host is behind **Cloudflare**, which 403s the default
`Python-urllib` User-Agent — so every request sends a real `User-Agent` (override via
`SHARADAR_USER_AGENT`). Incremental (M1-01 append-only — restatements arrive as new rows):
SEP/SF1/TICKERS carry `lastupdated`; **ACTIONS/SP500 do not** — ACTIONS tops up by `date.gte`, SP500
refetches whole. **Rate limit ≈ 500 req / 900s**, surfaced via `RateLimit-Remaining`/`RateLimit-Reset`
headers — the fetcher paces `SHARADAR_PACE_SEC` (default 0.05s) between calls and sleeps to the window
reset when the remaining budget runs low. The heavy `TICKERS`/`SP500` snapshots are **skipped on warm
runs** when their file is younger than `whole_refresh_days` (default 7). `incremental:false` forces a
full refetch.

**Ticker normalization:** the universe in `config/sharadar.json` is shared with EODHD, which writes
share classes with a dash (`BRK-B`); Sharadar uses a dot (`BRK.B`). `"ticker_replace": ["-","."]`
maps dash→dot for the API filter and the output filename (a no-op for the ~500 dash-free names);
`"ticker_overrides": {}` handles one-offs. Downstream joins should key on `permaticker`, not the raw
ticker (tickers are reused over time).

## Sharadar storage layout on the volume

```
code/                         uploaded fetcher (fetch_nasdaq.py, bootstrap.sh, sharadar.json) — skipped by download.sh
data_nasdaq/
├── SEP/<T>.json              D-12 per-ticker prices (closeunadj=raw, closeadj=fully adjusted)
├── SF1/<T>.json              D-05/06 per-ticker as-reported quarterly (ARQ) fundamentals
├── ACTIONS/<T>.json          D-04 per-ticker corporate actions
├── TICKERS/SHARADAR.json     D-13 whole-table entity master (permaticker, incl. delisted)
├── SP500/SHARADAR.json       D-15 whole-table S&P 500 add/remove history
├── _run.json                 run manifest (vendor, provenance, per-(table,ticker) results)
└── logs/                     gated by STORE_LOGS (errors/crashes always logged)
```

## Sharadar verify-at-implementation (spec §8)

Verified live against a subscribed key (2026-07): SF1 `ARQ` returns full as-reported history
(AAPL 112 quarters), COGS is the `cor` field, SF1's filing date is `date`, TICKERS carries delisted
names (survivorship-free). Still confirm downstream: the `ACTIONS`/`SP500` `action` code sets —
enumerate `DISTINCT action` before hard-coding any code→`action_type` map (unmapped codes logged,
never dropped); and SP500 `MIN(date)` (the "1957" claim).

---

# Tiingo (`scripts/launch.sh tiingo`)

The spec (§1, D-12) keeps Tiingo as the **optional tertiary cross-check** vendor: the tie-breaker
when EODHD (primary) and Sharadar SEP (secondary) disagree by >25 bps, plus a G-04 option for
pre-Dec-2020 news (paid add-on, off by default). `src/fetch_tiingo.py` writes to the `data_tiingo/`
namespace on the same volume.

| Spec item | Tiingo endpoint | Dataset / output |
|---|---|---|
| **D-12** tertiary price cross-check (+D-01/02/03 fields) | `/tiingo/daily/{T}/prices?startDate=` | `prices` → `data_tiingo/<T>.json` |
| **D-13** coverage cross-check | `/tiingo/daily/{T}` | `metadata` → `data_tiingo/metadata/<T>.json` |
| **D-09** SPY cross-check | `/tiingo/daily/SPY/prices` | `market` → `data_tiingo/market/SPY.json` |
| **D-13** full inventory incl. delisted | `supported_tickers.zip` (static CDN) | `symbol_list` → `data_tiingo/symbols/supported_tickers.json` |
| **G-04** pre-2020 news (paid add-on) | `/tiingo/news?tickers=` | `news` → `data_tiingo/news/<T>.json` (append-only) |

**API mechanics** (`fetch_tiingo.py`): `Authorization: Token …` header; each `prices` row carries
unadjusted OHLCV **and** `adjOpen/adjHigh/adjLow/adjClose/adjVolume` + `divCash` + `splitFactor`
(cross-check factor = `adjClose/close`). Ticker format uses dashes (`BRK-B`) — same as EODHD, so the
universe is shared verbatim. **Free tier ≈ 50 req/hr, 1,000 req/day, 500 unique symbols/month per
account** — the fetcher paces via `min_request_interval_sec` (72 s ≈ 50/hr, enforced PER token),
soft-caps a run via `max_requests_per_run` (jobs past the cap log `DEFER`, non-fatal), and resumes
across launches via `skip_fresh_days` (skip files refreshed <N days ago). A 429 sleeps out the
hourly window and retries. `market` (SPY) is fetched FIRST so the budget never starves it.
**Two-account split:** with `TIINGO_API_TOKEN2` set, the first half of `stocks` is pinned to
token 1 and the second half to token 2 (positional and sticky within a month — the unique-symbol
cap is per account, so a ticker must not switch accounts mid-month), interleaved for ~100 req/hr
combined: 252 + 251 symbols + SPY keeps both accounts under the cap and the whole universe
completes in a single ~5 h run.

## Tiingo storage layout on the volume

```
code/                          uploaded fetcher (fetch_tiingo.py, bootstrap.sh, tiingo.json) — skipped by download.sh
data_tiingo/
├── <T>.json                   D-12 per-ticker prices (unadjusted + adjusted + divCash + splitFactor)
├── metadata/<T>.json          D-13 cross-check entity/coverage snapshot
├── market/SPY.json            D-09 cross-check
├── symbols/supported_tickers.json  D-13 whole-inventory snapshot (incl. delisted)
├── news/<T>.json              G-04 optional (paid) — off by default
├── _run.json                  run manifest (requests_used, deferred count, per-job results)
└── logs/                      gated by STORE_LOGS (errors/crashes always logged)
```
