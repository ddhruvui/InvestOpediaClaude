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

---

# Borrow — D-10 (`scripts/launch.sh borrow`)

`src/fetch_borrow.py` + `config/borrow.json` → `data_borrow/`. **No API key.** Two free sources,
both verified live 2026-08-11.

| Spec item | Source | Output | Notes |
|---|---|---|---|
| **D-10** borrow snapshot (all shortable names) | `ftp://shortstock@ftp2.interactivebrokers.com/usa.txt` | `data_borrow/USA/<DATE>T<HHMMSS>.json.gz` | ~19.7k rows/day, ~0.9 MB gzipped. One immutable file per vendor publication |
| **D-10** borrow **history** | `https://www.iborrowdesk.com/api/ticker/<T>` | `data_borrow/history/<T>.json` | rolling ~1 y of daily rows, merged append-only by date |

**Access gotchas that will cost you an afternoon if you rediscover them:**

* The spec's `ftp3.interactivebrokers.com` **times out**. `ftp2` (and the unnumbered `ftp`) serve
  the same file anonymously. `IBKR_FTP_HOSTS` is tried in order.
* iBorrowDesk answers **only** on the `www.` host — the apex completes TLS then returns an empty
  reply with no redirect — and **only** with a browser `User-Agent`; programmatic UAs get 403.
* IBKR spells class shares with a **space** (`BRK B`, `BF B`); the parser normalises to the dash form.
* The file is pipe-delimited with a `#BOF|date|time` header and a `#EOF` trailer. **A file without
  `#EOF` is a truncated download and is not stored** — freezing a partial borrow snapshot is
  unrecoverable in exactly the way G-05 warns about.
* `FEERATE` is percent/year → `fee_bps_yr = FEERATE * 100`. Missing fees are stored as **NULL, not
  50**: the GC-50 default is a modelling fallback, and baking it in here would make a guess
  indistinguishable from a quote. Measured on the current universe: median **28.9 bps**, and only
  6 of 502 names sit above the spec's 50 bps default.

**This changes G-05.** The spec says borrow history "only accrues forward". iBorrowDesk's rolling
~1-year window means a missed day is recoverable for ~12 months, and the whole pinned test window
was backfillable on day one. The daily merge is still what turns a rolling window into permanent
history — and the IBKR snapshot half genuinely has no history at all.

---

# Calendar — D-11 / Q-001 (`scripts/launch.sh calendar`)

`src/fetch_calendar.py` + `config/calendar.json` → `data_calendar/XNYS.json`. Free, no key.
**The only fetcher with a pip dependency** (`exchange_calendars`); `launch.sh` sets `PIP_PACKAGES`
and `bootstrap.sh` installs it.

Emits `sessions` (past **and future** — 852 sessions ahead of today, which is what the `t+h+1`
vertical MOO has to be scheduled against), `early_closes`, `session_open_close`, and the
`package_version` that produced them (the calendar is only reproducible alongside its version;
feeds `config_hash`, G-10).

`fetch.py` reads this file (`SESSIONS_PATH`) to drive the `eod_bulk` loop. Without it the loop
falls back to Mon–Fri, which is how ten market-holiday day-files full of OTC/foreign rows ended up
on the volume — EODHD *answers* on holidays (Labor Day 2025: 4,418 rows), so the price feed cannot
tell you the exchange was shut. Those ten files are still there and should be deleted; every one
is a non-session confirmed against XNYS.

---

# Pod logs

Every pod tees its whole stdout to `_pod_logs/<UTC>-<script>-<podid>.log` on the volume. The pod
deletes itself when it finishes, taking its container log with it, so without this a failure
*before* the fetcher's own logging (bad `FETCH_SCRIPT`, failed pip install, unwritable `DATA_DIR`,
an import error at module scope) leaves no trace at all — the pod just vanishes having written
nothing.

> **RunPod S3 quirk:** a non-recursive `aws s3 ls s3://<vol>/<prefix>/` can return **nothing** for a
> prefix a pod created recently, while `--recursive` from the bucket root lists every key. Always
> verify with `--recursive` before concluding a job wrote nothing. (`download.sh` already works
> around the sibling flakiness in listing.)

---

# Reading this data correctly (before you build features on it)

Five rules the raw files do not enforce. Each one has bitten this dataset already.

**1. The session grid is `data_calendar/XNYS.json`, not the union of dates in the price files.**
`eod_bulk/US/` currently holds **10 day-files for NYSE holidays** (written before the D-11 guard
existed) containing only OTC/foreign rows — Labor Day 2025 has 4,418. They contain **zero** universe
names, so they cannot corrupt a per-ticker panel, but a naive `glob + union of dates` invents ten
phantom sessions. Filter every date against `sessions`, or delete those ten files.

**2. Raw close: prefer Sharadar `SEP.closeunadj` (or Tiingo `close`) over EODHD `close`.**
EODHD rewrites `close` retroactively after corporate actions — DD on 2025-07-28 reads 31.3937 from
EODHD versus an actual print of 75.06 (Sharadar and Tiingo agree on 75.06). So `F_t =
adjusted_close / close` off EODHD is wrong by ~2.4x for that name's entire pre-spinoff history.
Affected in the current window: **DD** and **CMCSA** (both spinoffs), 183 of 126,814 compared
closes breach 25 bps. EODHD's value is *breadth* (~45k tickers/day incl. delisted); Sharadar's is
*correctness*.

**3. `eod_bulk` day-files are NOT immutable.** The same date pulled at two times can differ:
CMCSA 2025-07-28 was 33.53 in the file pulled 2026-07-28 and 31.4246 when re-pulled 2026-08-11.
The resume logic ("skip any existing day-file") therefore freezes a **mix of vintages**. Treat a
day-file as "EODHD's view as of its pull date", not as ground truth.

**4. Sharadar keeps duplicate `(ticker, date)` rows on purpose** — that is M1-01 append-only, with
restatements distinguished by `lastupdated`. AAPL's SEP file has 272 rows over 261 distinct dates.
**SEP/SF1: dedupe to `max(lastupdated)` per key before use**; the extra rows are the audit trail,
not the panel. For PIT correctness (T-11), SF1 must be filtered to rows whose `lastupdated` is
`<= t`, not just `datekey <= t`.

**5. Sharadar SF1/SEP/ACTIONS are ticker-filtered to the 503 *current* names — they are NOT
survivorship-free.** The ACTIONS census contains **zero** `delisted` rows for exactly this reason.
Survivorship-free sources on the volume today are `eod_bulk`, `TICKERS`, `SP500`, and
`data/symbols/US.json`. Any universe construction or delisting analysis must come from those.

**Delisting fixture (T-12):** `EA` stopped trading after 2026-08-05 and both EODHD and Sharadar
agree — a ready-made real delisting to test against.


---

# Validation — D-12 / M1-04 + Q-004 (`scripts/launch.sh validate`)

`src/validate.py` → `data_quality/`. No API calls, no credits, no key. **Run after the nightly
`all`**, never inside it: it consumes the other jobs' output and `all` launches pods in parallel.

    data_quality/quarantine.json   the M1-04 list a consumer should honour
    data_quality/report.json       every check, with counts and offending keys

**Checks:** cross-vendor close (EODHD vs Sharadar `closeunadj` vs Tiingo, >25 bps ⇒ quarantine),
missing bars vs the D-11 calendar, non-session rows, duplicate `(ticker,date)`, split sanity,
stale feeds, and ticker reuse.

**Two judgement calls it encodes**, both learned the hard way:

* *When-issued vs recycled symbol.* Rows before the entity master's `firstpricedate` are either a
  few legitimate when-issued prints (GEV, CEG, VLTO, SOLV: 3–10 sessions immediately before the
  listing, which Sharadar omits and EODHD keeps) or a block from a **different issuer** (TKO: 534
  rows; SW: 339). The discriminator is the *gap* to the listing date, not the existence of early
  rows. Only the latter is quarantined.
* *Split sanity needs the ratio, not the move.* A 20%-move trigger fires on ordinary earnings gaps
  — SMCI alone has 16 in five years — and flagged nine false positives (NFLX −35% on the subscriber
  miss, APP +46% on earnings) simply because they landed near 2⁄3 or 1.5. Candidate ratios therefore
  exclude everything inside [0.55, 1.9]: only moves a market essentially never makes are evidence of
  a missing split.

**`--repair`** fixes what can be fixed from data already on the volume: per-ticker `eod` holes
filled from the corresponding `eod_bulk` day-file (EODHD's own two endpoints disagree — `eod/URI`
was missing 2023-04-06 while `eod_bulk/2023-04-06` had it at 355.27, matching Sharadar exactly), and
pre-listing rows from a recycled symbol dropped.

> **Repairs to `data/` are re-applied, not permanent.** `eod` is a full refetch every run, so the
> next EODHD pass restores the vendor's version. That is deliberate — the raw landing zone stays
> vendor-faithful — but it means `validate --repair` belongs *after* each nightly run.
> `quarantine.json` is the durable artefact; the §4 parse layer should consume it.

**Quarantine snapshot (34 tickers, taken 2026-08-13 on the then 5-year window).** The spans below start at 2021-07-28 because that was the window start; after the widen to 2000 they extend back further. Re-run `launch.sh validate` for the current list — this table is a record, not live state. Six are systematic — EODHD's pre-spinoff `close` is rescaled,
so use Sharadar `closeunadj` over these spans:

| ticker | span | rows | worst |
|---|---|---|---|
| HON | 2021-07-28 → 2026-06-26 | 2,468 | 10,233 bps |
| CMCSA | 2021-07-28 → 2026-01-02 | 2,228 | 670 bps |
| DD | 2021-07-28 → 2025-10-31 | 2,144 | 13,909 bps |
| LEN | 2021-07-28 → 2025-01-17 | 1,748 | 74 bps |
| J | 2021-07-28 → 2024-09-27 | 1,596 | 87 bps |
| LH | 2021-07-28 → 2023-06-30 | 970 | 1,640 bps |

The other 26 are isolated single-day disagreements (71 rows total) — ordinary vendor glitches.


---

# M1 landing layer (`src/build_m1.py`)

Raw vendor JSON → the M1 tables as Parquet. Needs pandas + pyarrow (the fetchers stay stdlib-only;
this is not a fetcher, so it runs locally or anywhere with the volume mirrored).

| output | rows built from the current volume |
|---|---|
| `raw_prices_eod/` (partitioned by year) | 628,270 — PK `(date, ticker)`, **0 duplicates** |
| `fundamentals_pit.parquet` | 1,031,271 long-format rows, 97 items |
| `corporate_actions.parquet` | 16,911 |
| `adjustment_factors/` | 625,255 |
| `borrow_fees.parquet` | 87,441 |
| `entities.parquet`, `sessions.parquet` | 74,956 / 7,795 |
| `qlib/<TICKER>.csv` | 503 — `date,open,close,high,low,volume,factor` |

**It enforces the consumption rules instead of restating them.** Verified on the current data:

* **Raw close provenance** — 622,723 rows take Sharadar `closeunadj`, 5,547 fall back to EODHD.
  DD 2025-07-28 lands at the true **75.06**, not EODHD's retro-rescaled 31.39.
* **Split vs spinoff** — 18 of EODHD's 73 "splits" retyped as `spinoff`, so Q-002 never treats a
  spinoff as a share-count change.
* **Vintages** — 980 (ticker, period) pairs carry more than one `lastupdated`, all preserved.
* **permaticker** — 0 nulls (needs the dot/dash alias: Sharadar writes `BRK.B`, everything else `BRK-B`).

> **The spec's fundamentals PK is wrong and this proves it.** §3 gives
> `(ticker, fiscal_period, filing_datetime, item)`, but M1-01 in the same section mandates keeping
> every restatement vintage — and **95,159 rows share that four-column key**, differing only by
> `lastupdated`. The PK here is five columns; with `lastupdated` added it is unique (0 duplicates).
> A T-11-correct read filters on `lastupdated <= t` **and** `filing_datetime <= t`.

> **`quarantined=True` does not mean "unusable"** once rule 2 has run. It means the vendors
> disagreed on that span and Sharadar's raw print was used. DD carries the flag *and* the correct
> price. Only a row still sourced from EODHD inside a tainted span has its close dropped.
