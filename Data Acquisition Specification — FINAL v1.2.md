# Data Acquisition Specification — FINAL v1.2 (coding-ready)

**For:** Implementation Blueprint v1.0.1, module M1 (Data Ingestion & Storage) and Q-001.
**Supersedes:** _EODHD-Primary Data Acquisition Specification_ (research draft) and _Data Download Manifest v1.0_ — both are merged here; where they conflicted, the manifest's blueprint-verified decisions win.
**Completeness claim:** The blueprint's entire external-data surface is the nine M1 tables + the Q-001 trading calendar + two model/ops artifacts (FinBERT weights, broker session). Every column of every M1 table is mapped below to a vendor field or to a blueprint-sanctioned fallback (§6). Closure proof in §7. Verified against vendor docs 2026-07-17; residual verify-at-implementation items in §8.
**Date:** 2026-07-17
**Changelog v1.0 → v1.1 (same day):** removed every dependency on holding an Interactive Brokers **account**. D-10 borrow collection now runs on the **public, no-login** IBKR short-stock file + iBorrowDesk (verified account-free). The ES-futures hedge option was dropped. Execution changed from fixed `IBKR (ib_async)` to a **BrokerAdapter interface** with `exec.broker: req`. §10-loggable deviation on `exec.broker`/§J code targets only — **no data item (D/G) changes**.
**Changelog v1.1 → v1.2 (same day):** user is a **US resident** — v1.1's Canadian-residency broker analysis is superseded. §1a rewritten for US eligibility: IBKR restored as the blueprint-default execution candidate (free to open and hold; **API requires IBKR Pro** — Lite has no API access); Alpaca re-enters as the fully-free alternative (its restriction excluded only Canadian residents). ES hedge option back to [MAY] iff `exec.broker = IBKR Pro` with futures permissions. PDT/G-14 applies as originally designed. **No data item (D/G) changes.**

---

## §1. Vendor stack, accounts, credentials

| Vendor                       | Product                                                                                                 | Role                                                                                                                                          | Auth / access                                                                                                                                    |
| ---------------------------- | ------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| EODHD                        | **All-In-One** (~$100/mo; user already subscribed)                                                      | Prices, splits/divs, earnings, news, S&P constituents, symbol lists                                                                           | `EODHD_API_TOKEN` env var; REST, JSON                                                                                                            |
| Nasdaq Data Link             | **Sharadar US Equities bundle** (SEP + SF1 + ACTIONS + TICKERS + SP500 + …)                             | PIT fundamentals (mandatory per §3.1 `data.vendors: sharadar+eodhd`), corporate-action detail, entity master, second-vendor price cross-check | `NASDAQ_DATA_LINK_API_KEY`; datatables REST + bulk export. _Exact bundle packaging/price login-gated — confirm at purchase (single-source flag)_ |
| IBKR public short-stock file | Free — **no IBKR account required**                                                                     | Borrow fees/availability (D-10)                                                                                                               | Anonymous FTP: `ftp3.interactivebrokers.com`, username `shortstock`, blank password (publicly accessible)                                        |
| Execution broker             | `req` — IBKR **Pro** is the blueprint default if accessible; else Alpaca (free) or another US candidate | M18 order placement, fills, positions/NAV for reconciliation; ES history [MAY] iff IBKR Pro                                                   | Via `BrokerAdapter` interface — candidates & capability checklist in §1a                                                                         |
| iBorrowDesk                  | Free                                                                                                    | Partial historical/indicative IBKR borrow rates per ticker                                                                                    | Public JSON endpoints                                                                                                                            |
| PyPI (free)                  | `exchange_calendars` (or `pandas_market_calendars`)                                                     | Q-001 NYSE sessions, past **and future**                                                                                                      | pip                                                                                                                                              |
| Hugging Face (free)          | `ProsusAI/finbert` weights                                                                              | F9 local sentiment scoring                                                                                                                    | one-time model pull, pinned revision hash                                                                                                        |
| Tiingo (free tier, optional) | EOD prices                                                                                              | Tertiary cross-check vendor                                                                                                                   | API key                                                                                                                                          |

Not data feeds, but config constants to hard-code and verify at go-live: opening-order **cutoff times** as exposed by the chosen broker (M18-3); user-supplied `tax.ordinary_rate` (jurisdiction-dependent — G-14's short-term-gains framing is US; set per your own situation), `pdt.account_equity`.

### §1a. Execution broker (US resident) — BrokerAdapter, IBKR default restored

M18 stays coded against a thin `live/broker_adapter.py` interface (G-15 spirit: one adapter, swappable). **Required capabilities for any candidate:** (1) API order placement — market, limit, stop, GTC; (2) margin account able to **short SPY** (default hedge, M14-02; $2,000 regulatory minimum equity applies to any margin account); (3) positions/fills/cash endpoints for EOD + pre-open reconciliation; (4) paper/sandbox environment (BP15). **Preferred:** MOO/OPG time-in-force; if absent, fallback = market order at the open, slippage measured vs the D-01 official open print, [IMPL]-logged.

- **IBKR (blueprint default, `ib_async`)** — free to open and hold: no account minimum, no inactivity or maintenance fees (inactivity fee eliminated 2021). **The API requires IBKR Pro — IBKR Lite has no API access** (per IBKR's own documentation). Pro carries no subscription fee; you pay commissions only: US stocks $0.005/share, **$1 minimum per order**, capped at 1% of trade value (Fixed; Tiered from ~$0.0035/share + pass-throughs). Market-data subscriptions are optional and **not needed** for this system (EODHD is the data plane; MOO orders need no live quotes; delayed data is free). Full order-type set incl. MOO/LOO and GTC stop/limit; ES futures available with permissions ([MAY] restored iff this broker).
- **Cost-realism check (G-07/BP5):** the $1/order minimum binds for small accounts. Per-order notional ≈ NAV / n_positions; $1 ≈ 15 bps at ~$667/order and ≈ 5 bps at $2,000/order. With ~35–100 positions, NAV below roughly $25k–$70k pushes realized per-trade cost toward the 30 bps grid point — report the {5,15,30} bps sensitivity honestly rather than assuming the anchor.
- **Alpaca** — US residents eligible (its restriction excluded only Canadian residents, which is what ruled it out under the prior location assumption): commission-free US stocks/ETFs, API-first, native **MOO via `time_in_force=opg`**, GTC stop/limit and bracket orders, shorting on margin (easy-to-borrow list), free unlimited paper trading. **The fully-free execution path** if IBKR remains unavailable for any non-cost reason. PDT applies at Alpaca like any US broker-dealer.
- **Other US-eligible candidates:** tastytrade API (+sandbox), Webull OpenAPI (US), Schwab Trader API, Tradier — run each through the capability checklist before committing.
- **Compliance (not legal/tax advice):** PDT (G-14) applies at all US broker-dealers when account equity < $25k — the blueprint's counter/defer logic runs exactly as designed. G-14's US short-term-capital-gains framing applies; `tax.ordinary_rate` remains user-supplied.

---

## §2. Master download registry (D-items)

Format per item: endpoint → request template → response→M1 field map → cadence → credit cost → caveats. All EODHD calls append `api_token=$EODHD_API_TOKEN&fmt=json`. All Sharadar calls: `https://data.nasdaq.com/api/v3/datatables/SHARADAR/{TABLE}.json?api_key=$NASDAQ_DATA_LINK_API_KEY` (+ `qopts.export=true` for full-table zip backfills; incremental via `lastupdated.gte=`).

### D-01 Daily prices (unadjusted OHLCV + adjusted close) → `raw_prices_eod`, input to Q-002

- **Backfill (per trading day, whole exchange):** `GET https://eodhd.com/api/eod-bulk-last-day/US?date=YYYY-MM-DD` — 100 credits/day-file. Loop over Q-001 sessions for the full span (≥ 2000-01-01; majors reach 30+ yr).
- **Per-ticker top-up / verification:** `GET https://eodhd.com/api/eod/{TICKER}.US?from=&to=&period=d` — 1 credit.
- **Field map:** `date→date`, `open/high/low/close/volume` → same (close is **unadjusted**; set `close_unadj_flag=true`), `adjusted_close` → staging column `adj_close_vendor` (used only to derive/verify Q-002 factors: `F_t = adjusted_close/close`). `vwap` → absent; leave NULL (proxy sanctioned, [IMPL-23]).
- **Cadence:** daily incremental = 1 bulk call after EODHD's post-close update (NYSE/Nasdaq ~15 min after close). The next morning's pull of day-t open is the **official open print** used for M18 slippage — no separate feed.
- **Caveats:** include delisted tickers (they remain in bulk files historically); dedupe on `(date,ticker)`.

### D-02 Splits → `corporate_actions(action_type='split')`

- `GET https://eodhd.com/api/splits/{TICKER}.US?from=&to=` — 1 credit; bulk daily: `eod-bulk-last-day/US?type=splits` (100).
- **Map:** `date→date`, parse `split` string `"A/B"` → `ratio=A/B`.

### D-03 Cash dividends → `corporate_actions(action_type='div_cash')`

- `GET https://eodhd.com/api/div/{TICKER}.US?from=&to=` — 1 credit; bulk daily: `type=dividends` (100).
- **Map:** `date` (ex-date) → `date`; `value` (per-share, **unadjusted** for Q-002's `div/raw_close_{ex−1}` step — use `unadjustedValue` if both present) → `amount`; keep `paymentDate/recordDate/declarationDate` as extra columns when present (payment-date coverage weaker — non-blocking; cost model's short-dividend liability keys off ex-date).

### D-04 Stock dividends, spinoffs, delistings (with reasons), ticker changes → `corporate_actions(div_stock|spinoff|delist)` + entity events

- **Sharadar `ACTIONS`**: filters `ticker=`, `date.gte=`; columns `date, action, ticker, name, value, contraticker, contraname`. Action codes include `split, dividend, spinoffdividend, delisted, listed, tickerchangefrom/to, mergerfrom/mergerto, …` (enumerate full code list from table docs at implementation; map codes → blueprint `action_type`, unmapped codes logged not dropped). History to 1998, 21k+ active+delisted tickers.
- **Cadence:** full export backfill once; daily incremental by `lastupdated`.
- **Why not EODHD:** no spinoff/stock-dividend feed — this table is the audit's added requirement.

### D-05 PIT fundamentals (as-reported, filing-dated) → `fundamentals_pit`

- **Sharadar `SF1`**, **dimension `ARQ`** (as-reported quarterly; original filing values are preserved by the AR dimension itself — restatements live in `MRQ/MRY`, which MAY be stored alongside for reference, never for features).
- Request: `SHARADAR/SF1.json?dimension=ARQ&ticker=…&calendardate.gte=…`; backfill via export; incremental by `lastupdated.gte`.
- **Field map:** `ticker→ticker`, `reportperiod→fiscal_period`, `calendardate→report_date` (period-end anchor), **`datekey→filing_datetime`** (SEC filing date; M2-03 adds the +1-session visibility lag — do not add extra lag here), each indicator column → long rows `(item,value)`. ~28 yr history, 16k+ companies, survivorship-bias-free.
- **Feeds F7 items:** revenue, cogs, netinc, equity (book), assets, workingcapital/Δ, depamor, eps, shareswa/sharesbas — sufficient for E/P, B/M, S/P, ROE, gross-profitability, asset-growth, Sloan accruals.
- **EODHD `GET /api/v1.1/fundamentals/{T}.US`** (10 credits) is **cross-check only** (single mutable record ⇒ cannot satisfy M1-01/T-11; see §6 G-06).

### D-06 Shares outstanding (PIT) → input to F6 `turnover`, F7 `mcap`

- Primary: SF1 ARQ `shareswa`/`sharesbas` (filing-dated, from D-05 — no separate pull). Cross-check: EODHD fundamentals `outstandingShares` (annual/quarterly arrays).

### D-07 Earnings events + EPS actual/consensus → `earnings_calendar`

- **History:** EODHD `GET /api/v1.1/fundamentals/{T}.US?filter=Earnings::History` (within the 10-credit fundamentals call) — fields `reportDate→announce_datetime(date)`, `beforeAfterMarket→flag` (`BeforeMarket|AfterMarket|null`; null → treat as AMC, conservative, log), `epsActual→eps_actual`, `epsEstimate→eps_consensus`, `epsDifference`, `surprisePercent`. Depth: from inception for major names.
- **Upcoming (Q-004 coverage check + F8 `days_to_earnings`):** `GET https://eodhd.com/api/calendar/earnings?from=&to=&symbols=` — treat as 1 credit (verify §8).
- **`est_stddev`, `n_estimates`:** NOT available historically → §6 G-02 (seasonal-diff SUE fallback is the historical path). Forward-collection per D-14 populates them from go-live (`earningsEstimateNumberOfAnalysts`; σ proxy from Low/High if used — logged [IMPL]).

### D-08 News headlines → `news_headlines`

- `GET https://eodhd.com/api/news?s={TICKER}.US&from=&to=&limit=1000&offset=` — **5 credits/call**; paginate by offset.
- **Map:** published datetime (UTC) → `timestamp_utc` (drives F9's after-close→next-session roll), `title→headline`, `symbols→ticker(s)`, `link/source→source`; store `content` and vendor `sentiment{polarity,neg,neu,pos}` as optional columns (F9 scores locally with FinBERT; vendor sentiment = cross-check only).
- **Depth:** ~Dec-2020 onward (API launch — single-source; probe per §8). Pre-coverage F9 features are NaN by design (M4 NaN policy).

### D-09 Index prices (SPY) → `index_prices`, input to Q-019

- EODHD `GET /api/eod/SPY.US` (+ `GET /api/div/SPY.US` for the total-return build). 30+ yr. **ES hedge option = [MAY], restored iff `exec.broker = IBKR Pro` with futures permissions** (history + execution via the broker); on any other broker it stays dropped and the default short-SPY hedge applies (§1a checklist).

### D-10 Borrow fees/availability → `borrow_fees`

- **Forward daily — no IBKR account required:** the IBKR shortable-stock file is publicly accessible via **anonymous FTP** (`ftp3.interactivebrokers.com`, username `shortstock`, blank password, file `usa.txt`) → per-ticker indicative fee + available quantity; supplement with iBorrowDesk JSON (free; partial multi-year indicative history). EODHD carries no borrow data (confirmed) — this public route + the GC-50 default is the free path. If `exec.broker = IBKR Pro`, the same SLB data is additionally available through the account API — optional redundancy, not a requirement.
- **Map:** fee → `fee_bps_yr`; missing → **GC default 50** (schema-sanctioned). Start this collector on day 1 of the build — history only accrues forward (§6 G-05).

### D-11 Trading calendar (Q-001)

- `exchange_calendars`: `xcals.get_calendar("XNYS")` → sessions (incl. half-days), past + **future** (required to schedule the `t+h+1` vertical MOO). Pin package version; refresh on upgrade. Cross-check: EODHD exchange-details holidays. Zero credits.

### D-12 Second-vendor price cross-check (M1-04, [IMPL-29])

- **Sharadar `SEP`**: `ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated`; ≥1998; 20k+ names incl. delisted.
- Job: rotating 5% universe sample per pull; compare close (and closeadj-implied factor) vs D-01; discrepancy > 25 bps → quarantine ticker, block from universe mask until resolved. Tiingo = tertiary tie-breaker.

### D-13 Entity master / permanent id

- **Sharadar `TICKERS`**: `permaticker` (M1 primary entity key), `ticker, name, exchange, isdelisted, category, cusips, siccode, sector, industry, firstpricedate, lastpricedate, …`. Join D-01/D-04 symbol-change events through `permaticker`. EODHD `/api/id-mapping` + exchange symbol-change history = secondary. Also pull `GET https://eodhd.com/api/exchange-symbol-list/US?delisted=1` once per day (ticker inventory incl. delisted; drives backfill completeness).

### D-14 Analyst-estimate snapshots (forward-collection) → `analyst_estimates`

- Daily job from go-live: EODHD `Earnings::Trend` (v1.1: quarterly/annual sub-objects) — `earningsEstimateAvg→consensus_fy1_eps`, `earningsEstimateNumberOfAnalysts→n_ests`, plus `epsTrendCurrent/7/30/60/90daysAgo`, revision counts, Low/High. Append-only snapshots (M1-01).
- **`rev_mom` (F8) is computable live from day 1**: interpolate `epsTrend60daysAgo/90daysAgo` at t−63; exact from own snapshots thereafter. Historical backfill only via Zacks/I-B/E-S (§6 G-03); historical `rev_mom` = NaN otherwise.

### D-15 Conditional: historical S&P 500 constituents → `index_constituents`

- Needed as a _feed_ only if `universe.mode = sp500_historical` (default `top1000_dollar_volume` **computes** membership from D-01/D-06 via Q-003/Q-010). Pull anyway (cheap, M1-03-ready): EODHD `GET /api/fundamentals/GSPC.INDX?filter=HistoricalTickerComponents` (10 credits) — `Code, StartDate, EndDate, IsActiveNow, IsDelisted` → dated intervals; from Jan 2000. Deeper: Sharadar `SP500` table (additions/removals, reportedly since 1957 — single-source flag).

### D-16 Model artifact: FinBERT weights (F9)

- One-time `ProsusAI/finbert` pull from Hugging Face; pin revision hash into `config_hash` inputs (G-10 reproducibility).

**Conditional (only if flags flip):** sector/industry for F10 IndClass (from D-13 `siccode/sector` — no new pull); vendor VWAP via EODHD intraday (5 cr) iff proxy rejected; ES futures (D-09 note); EODHD `/api/sentiments` (5 cr) as F9 cross-check.

**Confirmed NOT required (do not build):** Russell membership; VIX; Fama-French/UMD; risk-free rate (§16.2 Sharpe has no RF term); yield/credit/macro/economic-events; insider transactions; options/IV (G-13); EODHD historical-market-cap (mcap := `raw_close × shares_PIT`); EODHD Technical Indicators API (F1–F6 computed locally; canonical forms fixed in [IMPL-23]).

---

## §3. Landing schema & provenance (implements M1)

Blueprint M1 tables verbatim, plus mandatory provenance columns on every raw table: `data_snapshot_id` (= pull_date + SHA-256 of the pull manifest), `vendor`, `pulled_at_utc`, `source_endpoint`. Storage: immutable raw landing zone (verbatim JSON/CSV per pull) → parsed long-format Parquet partitioned by date, PK `(date,ticker)` (`fundamentals_pit`: PK `(ticker, fiscal_period, filing_datetime, item)`), entity key `permaticker`. **Append-only**: re-pulls insert new rows; nothing is ever updated in place (M1-01). SF1 revision rows are distinguished by `lastupdated`; ARQ originals are the feature source (T-11).

---

## §4. Ingestion pipeline

**Order (backfill):**

1. **Reference layer:** D-11 calendar → D-13 TICKERS + exchange-symbol-list(delisted=1) → D-15 constituents → D-04 ACTIONS full export. (~few hundred credits + Sharadar exports)
2. **Prices:** D-01 bulk loop over sessions (≈252 × years × 100 credits ⇒ e.g. 26 y ≈ 655k credits → spread over ~7 days under the 100k/day cap) + D-02/D-03 per-ticker for the assembled universe; D-12 SEP full export in parallel; D-09 SPY.
3. **Fundamentals/events:** D-05 SF1 ARQ export; EODHD fundamentals cross-check + D-07 Earnings::History for the universe (~3,000 × 10 = 30k credits ⇒ one day).
4. **News:** D-08 per universe ticker, 2020→now, paginated (budget ~25k–100k credits; 1–2 days).
5. **Go-live collectors switched on:** D-10 borrow (daily), D-14 Trend snapshots (daily), D-08 incremental (daily), D-01/02/03 bulk (daily), D-12 5% cross-check (per pull), D-13 symbol-list diff (daily).

**Daily steady-state:** 1×bulk EOD (100) + bulk splits/divs (200) + calendar (≈1) + news/sentiment increments + Trend snapshots ⇒ low thousands of credits — comfortably inside 100,000/day. **Pacing:** ≤1,000 req/min hard vendor limit → client-side token bucket at ~10–15 rps sustained; retry 429/5xx with exponential backoff + jitter, max 5 attempts, then dead-letter + health flag. All fundamentals-class endpoints cost **10 credits**; news/technical/intraday **5**; bulk EOD **100**; plain EOD/splits/div **1**.

**qlib bridge (unchanged):** per-ticker CSVs `date,open,close,high,low,volume,factor` (factor = Q-002) from the adjusted store → `scripts/dump_bin.py dump_all … --include_fields open,close,high,low,volume,factor` → `qlib.init(provider_uri=…, region=REG_US)`; never qlib's default Yahoo bundle; run `check_data_health.py` before modeling.

---

## §5. Validation hooks (ties into Q-004 / tests)

- **Schema probe (first task of implementation):** one minimal request per endpoint/table above; diff returned field lists against §2; any drift → update this doc before writing parsers.
- **M1-04 cross-check job:** as D-12; quarantine list feeds Q-004.
- **Q-004 dependencies:** missing-bars check ← D-01 vs D-11; split sanity ← D-01 raw jump vs D-02 ratio; adjusted/raw continuity ← D-01 factor curve; earnings-calendar coverage ← D-07 upcoming vs universe; delisted handling ← D-04/D-13 (T-12 fixture).
- **PIT test T-11 fixture:** pick one restated name; assert feature at t uses ARQ values with `datekey ≤ t−1 session` and that a later `lastupdated` row does not alter history.

---

## §6. Gap register (no clean download exists — blueprint-sanctioned fallbacks)

| #    | Gap                                                                     | Sanctioned handling (blueprint cite)                                                                                                       | Paid fix (optional)                  |
| ---- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------ |
| G-01 | `delist_return` (CRSP-style, incl. settlement value) — no retail vendor | M1-02: mark non-tradable on last date + log; never invent a haircut. Last-trade return computable from D-01/D-12                           | CRSP (academic)                      |
| G-02 | Historical `est_stddev`/`n_estimates` for primary SUE                   | F8 [IMPL] seasonal-diff SUE `(EPS_q−EPS_{q−4})/σ(8 seasonal diffs)` for the whole backtest; primary SUE from D-14 forward                  | Zacks / I/B/E/S                      |
| G-03 | Deep daily FY1-consensus history (`rev_mom`)                            | Live from day 1 via `epsTrend*daysAgo`; exact via D-14 forward; historical NaN → M4 NaN policy                                             | Zacks / I/B/E/S backfill             |
| G-04 | News before ~Dec 2020                                                   | F9 NaN pre-coverage; sentiment is a feature, never a strategy (F9)                                                                         | Tiingo News / GDELT (quality varies) |
| G-05 | Borrow-fee history                                                      | GC 50 bps default + HTB exclusion (M18-4); own table accrues from go-live                                                                  | Ortex / S3                           |
| G-06 | EODHD-only PIT fundamentals — structurally impossible                   | Not sanctioned: Sharadar SF1 required (D-05). Dropping it = §10-logged deviation + filing-date+2d lag + accepted restatement contamination | —                                    |

---

## §7. Completeness closure (why nothing is missing)

- **M1 tables ↔ D-items:** `raw_prices_eod`←D-01; `corporate_actions`←D-02/03/04 (+G-01); `fundamentals_pit`←D-05/06; `index_constituents`←D-15; `earnings_calendar`←D-07 (+G-02); `analyst_estimates`←D-14 (+G-03); `news_headlines`←D-08 (+G-04); `index_prices`←D-09; `borrow_fees`←D-10 (+G-05). Q-001←D-11. Every M1 column is either mapped above or in §6 with its fallback.
- **Feature blocks:** F1–F6←D-01 (+D-06 turnover); F7←D-05/D-06+D-01; F8←D-07/D-14 (+G-02/03); F9←D-08+D-16; F10←D-01 (+D-13 sector, VWAP proxy). Labels/barriers (M5)←D-01 via Q-002/014/016. M12/M13/M16 baselines←D-01/D-09 only (no factor data by construction of §16.2/G-08). M14 hedge←D-09 betas. M15/M18 compliance←D-03 (short div liability), D-10, user config. Slippage←D-01 next-day open.
- **Q-inventory:** every Q-001…Q-104 input resolves to {D-items, other Q-items, user config}. No external input remains unsourced.
- **De-scope proof:** each item in the NOT-required list has zero consumers in v1.0.1 (checked against F-blocks, all module Input lines, and §5.1 "Direct inputs" column).

---

## §8. Verify-at-implementation register (carried single-source / unverified details)

1. EODHD per-ticker **news depth** (launch-blog-dated ~Dec 2020) — probe oldest article per universe ticker.
2. **Sharadar bundle** exact packaging & current price (login-gated; the only public individual-price figure is dated).
3. Sharadar **SP500 table** 1957 start (secondary-source claim).
4. EODHD **calendar endpoint credit cost** (assumed 1) and bulk-endpoint activation status on the All-In-One plan.
5. `beforeAfterMarket` null-rate in Earnings::History (drives the conservative-AMC default).
6. D-03 `unadjustedValue` presence across the universe (else derive from `value` + factor curve).
7. Exact ACTIONS action-code enumeration; extend the code→`action_type` map before first parse.
8. Short-stock file format + anonymous-FTP access from your network, and the chosen broker's **opening-order cutoff** constants.
9. Whether the original IBKR blocker persists now that cost is ruled out (account is free; **Pro required for API**). If IBKR: confirm Pro Fixed-vs-Tiered all-in cost at your typical order size, MOO submission cutoff, and futures permissions if the ES [MAY] is wanted.
10. If Alpaca: confirm OPG (MOO) submission cutoff, SPY/single-name shortability handling, and GTC stop + limit (bracket) mechanics against the M5.2 barrier mapping.
11. Chosen broker's **SPY short/margin + GTC stop/limit** support and PDT handling (§1a checklist) before M18 coding starts.

_End of final data acquisition specification. Any change to §2/§6 after coding starts is a versioned edit (v1.3+) with a delta log, mirroring blueprint §10 discipline._
