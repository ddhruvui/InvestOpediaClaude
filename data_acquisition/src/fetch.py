#!/usr/bin/env python3
"""Download every EODHD dataset the v1.2 spec needs, for the universe in tickers.json.

Pure stdlib (no pip). Covers the ENTIRE EODHD column of
`../../Data Acquisition Specification — FINAL v1.2.md` (the D-items EODHD is the source or
cross-check for). Non-EODHD vendors in the spec — Sharadar, the IBKR short-stock FTP file,
FinBERT weights, `exchange_calendars`, iBorrowDesk, Tiingo — are SEPARATE pullers (different
auth + rate models) and are intentionally NOT handled here.

PER-EQUITY (one file per ticker, driven by "stocks" + "datasets"; default datasets ["eod"]):
    eod          -> DATA_DIR/<TICKER>.json               D-01  date/open/high/low/close/
                                                          adjusted_close/volume (close is UNADJUSTED;
                                                          F_t = adjusted_close/close is the Q-002 factor)
    dividends    -> DATA_DIR/dividends/<TICKER>.json      D-03  ex-date cash dividends (value = per-share,
                                                          unadjusted where EODHD exposes unadjustedValue)
    splits       -> DATA_DIR/splits/<TICKER>.json         D-02  split ratios "A/B"
    fundamentals -> DATA_DIR/fundamentals/<TICKER>.json   D-05/06 cross-check + D-07 Earnings::History.
                                                          Full lossless EODHD object (Highlights, SharesStats,
                                                          Earnings.History/Trend, General.Sector, Financials)
    estimates    -> DATA_DIR/estimates/<TICKER>.json      D-14  Earnings::Trend analyst snapshots, APPEND-ONLY
                                                          (one dated row per pull — M1-01 immutable history)
    news         -> DATA_DIR/news/<TICKER>.json           D-08  timestamped, ticker-tagged articles (HEAVY;
                                                          own "news_from" window — depth is ~Dec-2020 onward)

MARKET / INDEX / EXCHANGE LEVEL (these need ".INDX"/exchange symbols the per-equity form can't express):
    market            -> DATA_DIR/market/<SYMBOL>.json            D-09  SPY.US daily level (index_prices, Q-019)
    market_dividends  -> DATA_DIR/market/dividends/<SYMBOL>.json  D-09  SPY dividends (total-return build)
    index_constituents-> DATA_DIR/universe/<INDEX>.json           D-15  survivorship-free S&P membership
                                                                 (Components + HistoricalTickerComponents)
    exchanges         -> DATA_DIR/calendar/<CODE>.json            D-11  EODHD exchange holidays (calendar CROSS-CHECK
                                                                 to the exchange_calendars source of truth)
    symbol_lists      -> DATA_DIR/symbols/<CODE>.json             D-13  full exchange symbol inventory incl. delisted
                                                                 (drives backfill completeness)
    earnings_upcoming -> DATA_DIR/earnings/upcoming.json          D-07  forward earnings calendar for the universe
                                                                 (Q-004 coverage check + F8 days_to_earnings)

BULK BACKFILL (D-01 PRIMARY — the spec's survivorship-bias-free price backfill; config block "eod_bulk"):
    eod_bulk          -> DATA_DIR/eod_bulk/<CODE>/YYYY-MM-DD.json  whole-exchange OHLCV per trading day, ALL tickers
                                                                 INCLUDING delisted. Resumes newest-first across runs
                                                                 (skips existing day-files), bounded by max_days_per_run
                                                                 to stay under the 100k-credit/day cap.

INCREMENTAL (default on; set "incremental": false in the config to force a full refetch). The
network volume persists DATA_DIR between RunPod launches, so the *append-only* streams — **news**
and **estimates** — read what's already on disk and only add new rows (news: rows dated on/after the
latest stored row, merged+deduped; estimates: one snapshot per pull day). Everything else refetches
in full: eod / dividends / splits / market are tiny AND EODHD rewrites `adjusted_close` retroactively
after a split/dividend so a naive append would go stale; fundamentals / universe / calendar / symbol
lists / earnings-calendar are point-in-time snapshots replaced whole.

Self-termination is bootstrap.sh's job, so this runs/tests locally:

    DATA_DIR=./out CONFIG_PATH=config/tickers.json EODHD_API_TOKEN=xxx STORE_LOGS=true python src/fetch.py

Logging (env-controlled): `_run.json` manifest is always written. A full run log
(`logs/run-<ts>.log`) is stored ONLY when `STORE_LOGS` is truthy. Failures (`logs/error-<ts>.log`)
and crashes (`logs/crash-<ts>.log`) are ALWAYS logged, regardless of `STORE_LOGS`.
Exit code: 0 if every job succeeded, 1 otherwise.
"""
import http.client
import json
import os
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

TOKEN = os.environ.get("EODHD_API_TOKEN", "")
DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/tickers.json")
API = "https://eodhd.com/api"
EOD_FIELDS = ("date", "open", "high", "low", "close", "adjusted_close", "volume")
BULK_FIELDS = ("code", "date", "open", "high", "low", "close", "adjusted_close", "volume")  # + ticker key
PAGE_SIZE = 1000   # EODHD list-endpoint max page
MAX_PAGES = 100    # hard backstop so offset pagination can never loop forever
CAL_CHUNK = 100    # symbols per calendar/earnings call — keeps the URL well under any length limit
# Env-gated: when truthy, a run log is stored on success. Errors/crashes log regardless (see below).
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

_ctx = None  # default = verified TLS; falls back to unverified if CA bundle is missing
_LOG_LINES = []  # captured stdout, persisted to logs/ on success (if STORE_LOGS) or always on failure


def log(msg):
    """Print to stdout (pod container log) AND capture for the persisted log file."""
    print(msg, flush=True)
    _LOG_LINES.append(msg)


def _persist_log(kind, manifest=None):
    """Write captured output (+ manifest) to logs/<kind>-<UTCstamp>.log; return the path."""
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(log_dir, f"{kind}-{stamp}.log")
    with open(path, "w") as f:
        if _LOG_LINES:
            f.write("\n".join(_LOG_LINES) + "\n\n")
        if manifest is not None:
            f.write("--- manifest ---\n" + json.dumps(manifest, indent=2) + "\n")
    return path


def get_json(url):
    """GET url -> (http_status, parsed_json_or_None). Retries transient errors."""
    global _ctx
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(url, timeout=60, context=_ctx) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            return e.code, None
        except (ssl.SSLError, urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError) as e:
            # http.client.HTTPException covers RemoteDisconnected / IncompleteRead — common on long
            # news pagination where the server drops a connection mid-stream. Retry, don't fail the job.
            err = e if isinstance(e, ssl.SSLError) else getattr(e, "reason", e)
            if _ctx is None and isinstance(err, ssl.SSLError):
                print("WARN: TLS verification failed, retrying without verification", flush=True)
                _ctx = ssl._create_unverified_context()
                continue  # one-time TLS downgrade — does not consume a retry attempt
            if attempt < 4:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            raise


def _get(path, params):
    """GET {API}/{path}?params -> parsed JSON, raising on auth/transport failure."""
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    status, payload = get_json(url)
    if status == 401:
        raise RuntimeError("401 Unauthorized — EODHD plan inactive or token wrong")
    if status != 200 or payload is None:
        raise RuntimeError(f"unexpected response (status={status})")
    return payload


def _paginate(path, base_params, label=None):
    """Offset-paginate an EODHD list endpoint. Returns (rows, truncated).

    A multi-page pull (news can be 50+ pages / hundreds of MB) is the one place a run
    can sit silent for minutes, so when STORE_LOGS is on we emit a per-page heartbeat —
    it lands in the run log on success and in the error log if the pull dies mid-stream,
    pinpointing which page dropped.
    """
    rows = []
    offset = 0
    for page in range(1, MAX_PAGES + 1):
        params = dict(base_params, limit=PAGE_SIZE, offset=offset)
        payload = _get(path, params)
        if not isinstance(payload, list):
            raise RuntimeError(f"{path}: expected list, got {type(payload).__name__}")
        rows += payload
        if STORE_LOGS:
            log(f"     {path} {label or ''} page {page}: +{len(payload)} ({len(rows)} total)")
        if len(payload) < PAGE_SIZE:
            return rows, False
        offset += PAGE_SIZE
    return rows, True  # hit MAX_PAGES — more rows may exist


# --- low-level per-equity fetchers: (symbol, from_date) -> list to persist ----------------------

def _fetch_eod(symbol, from_date):
    # D-01: unadjusted OHLC + adjusted_close + volume. Delisted names stay in EODHD historically.
    params = {"api_token": TOKEN, "fmt": "json", "period": "d", "order": "a"}
    if from_date:
        params["from"] = from_date
    payload = _get(f"eod/{symbol}", params)
    if not isinstance(payload, list):
        raise RuntimeError("eod: expected list")
    # project to the canonical OHLCV field set (unadjusted OHLC + adjusted_close + volume)
    return [{k: row.get(k) for k in EOD_FIELDS} for row in payload]


def _fetch_eod_bulk_day(exchange, date):
    # D-01 backfill PRIMARY: whole-exchange OHLCV for one trading day (~100 credits/day-file).
    # Returns EVERY ticker that traded that day — INCLUDING delisted names — which the per-ticker
    # `eod` job (bounded to the fixed universe) can never reach. This is what makes the price
    # history survivorship-bias-free (spec §2 D-01, §7). `adjusted_close` here is as-of-pull and may
    # go stale after a later split/dividend — that's fine: the canonical Q-002 factor is derived from
    # D-02/D-03, and D-01's UNADJUSTED OHLCV (the source of truth) is immutable.
    params = {"api_token": TOKEN, "fmt": "json", "date": date}
    payload = _get(f"eod-bulk-last-day/{exchange}", params)
    if not isinstance(payload, list):
        raise RuntimeError("eod_bulk: expected list")
    return [{k: row.get(k) for k in BULK_FIELDS} for row in payload]


def _fetch_dividends(symbol, from_date):
    # D-03: ex-date, per-share value (EODHD exposes unadjustedValue on most names — kept as-is).
    params = {"api_token": TOKEN, "fmt": "json"}
    if from_date:
        params["from"] = from_date
    payload = _get(f"div/{symbol}", params)
    if not isinstance(payload, list):
        raise RuntimeError("dividends: expected list")
    return payload


def _fetch_splits(symbol, from_date):
    # D-02: split date + "A/B" ratio string.
    params = {"api_token": TOKEN, "fmt": "json"}
    if from_date:
        params["from"] = from_date
    payload = _get(f"splits/{symbol}", params)
    if not isinstance(payload, list):
        raise RuntimeError("splits: expected list")
    return payload


def _fetch_estimates(symbol, from_date):
    # D-14: point-in-time analyst-estimate snapshot. EODHD only ever returns the CURRENT Trend
    # object, so we stamp each pull with its UTC date and append (M1-01 immutable) — history accrues
    # forward from go-live, one row per pull day (from_date is intentionally ignored).
    payload = _get(f"fundamentals/{symbol}", {"api_token": TOKEN, "filter": "Earnings::Trend"})
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [{"date": stamp, "trend": payload}]


def _fetch_news(symbol, from_date):
    # D-08: paginated timestamped headlines. Depth is ~Dec-2020 onward (API launch).
    base = {"api_token": TOKEN, "fmt": "json", "s": symbol}
    if from_date:
        base["from"] = from_date
    rows, truncated = _paginate("news", base, label=symbol)
    if truncated:
        log(f"WARN news {symbol}: hit MAX_PAGES ({MAX_PAGES}) — older articles may be truncated")
    return rows


# --- snapshot fetchers (whole objects, always refetched) ----------------------------------------

def fetch_fundamentals(symbol):
    # D-05/06 cross-check + D-07 Earnings::History. Stored lossless: the full nested object
    # (General/Highlights/Valuation/SharesStats/Earnings/Financials/...). Downstream PIT extraction
    # selects the blocks it needs; keeping it whole avoids silently dropping a field a model wants.
    payload = _get(f"fundamentals/{symbol}", {"api_token": TOKEN})
    if not isinstance(payload, dict):
        raise RuntimeError("fundamentals: expected object")
    return payload


def fetch_index_constituents(index_symbol):
    # D-15: index fundamentals carry General + Components (current) + HistoricalTickerComponents
    # (add/remove dates) — the survivorship-free membership source. Stored lossless.
    payload = _get(f"fundamentals/{index_symbol}", {"api_token": TOKEN})
    if not isinstance(payload, dict):
        raise RuntimeError("index_constituents: expected object")
    return payload


def fetch_exchange_details(code):
    # D-11: EODHD exchange holidays — the cross-check to the exchange_calendars source of truth.
    payload = _get(f"exchange-details/{code}", {"api_token": TOKEN, "fmt": "json"})
    if not isinstance(payload, dict):
        raise RuntimeError("exchange_calendar: expected object")
    return payload


def fetch_symbol_list(code):
    # D-13: full symbol inventory for the exchange INCLUDING delisted tickers (delisted=1) —
    # drives backfill completeness / delisted handling (T-12).
    payload = _get(f"exchange-symbol-list/{code}", {"api_token": TOKEN, "fmt": "json", "delisted": 1})
    if not isinstance(payload, list):
        raise RuntimeError("symbol_list: expected list")
    return payload


def fetch_earnings_upcoming(symbols, from_date, to_date):
    # D-07 (upcoming): forward earnings calendar for the universe. `symbols` is chunked so the URL
    # stays well within limits; the per-chunk `earnings` arrays are concatenated.
    out = []
    for i in range(0, len(symbols), CAL_CHUNK):
        chunk = symbols[i:i + CAL_CHUNK]
        params = {"api_token": TOKEN, "fmt": "json", "symbols": ",".join(chunk)}
        if from_date:
            params["from"] = from_date
        if to_date:
            params["to"] = to_date
        payload = _get("calendar/earnings", params)
        rows = payload.get("earnings", []) if isinstance(payload, dict) else payload
        if isinstance(rows, list):
            out += rows
    return out


# Dedup keys for the append-only incremental streams (a stable per-record identity).
def _news_key(r):
    return r.get("link") or f"{r.get('date', '')}|{r.get('title', '')}"


def _estimates_key(r):
    return r.get("date")  # one snapshot per pull day; a same-day re-run overwrites it


# dataset -> (low_level_fetch, subdir, from_cfg_key, incremental_key_fn or None).
# incremental_key_fn is set ONLY for append-only streams; None => always full refetch (small /
# retroactively-revised: eod, dividends, splits).
SERIES = {
    "eod":       (_fetch_eod,       None,         "from",      None),
    "dividends": (_fetch_dividends, "dividends",  "from",      None),
    "splits":    (_fetch_splits,    "splits",     "from",      None),
    "estimates": (_fetch_estimates, "estimates",  "from",      _estimates_key),
    "news":      (_fetch_news,      "news",       "news_from", _news_key),
}
# dataset -> (fetch(symbol), subdir, count_fn). Point-in-time objects, always refetched whole.
SNAPSHOT = {
    "fundamentals": (fetch_fundamentals, "fundamentals", None),
}
VALID_DATASETS = set(SERIES) | set(SNAPSHOT)


def _read_existing(path):
    """Existing list payload on disk, or [] if absent/unreadable."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _latest_date(rows):
    dates = [(r.get("date") or "")[:10] for r in rows if r.get("date")]
    return max(dates) if dates else None


def _merge(existing, new, keyfn):
    """Union existing + new by keyfn (new wins on collision); sorted by date. Returns (rows, added)."""
    by = {keyfn(r): r for r in existing}
    added = 0
    for r in new:
        k = keyfn(r)
        if k not in by:
            added += 1
        by[k] = r
    rows = sorted(by.values(), key=lambda r: r.get("date") or "")
    return rows, added


def _write(out_path, data):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(data, f)


def main():
    if not TOKEN:
        print("FATAL: EODHD_API_TOKEN not set", file=sys.stderr)
        return 1
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    exchange = cfg.get("exchange", "US")
    datasets = cfg.get("datasets") or ["eod"]
    stocks = cfg.get("stocks", [])
    market = cfg.get("market", [])                       # fully-qualified symbols (SPY.US)
    market_dividends = cfg.get("market_dividends", [])   # SPY.US div for the total-return build
    index_constituents = cfg.get("index_constituents", [])
    exchanges = cfg.get("exchanges", [])
    symbol_lists = cfg.get("symbol_lists", [])           # D-13 exchange-symbol-list (incl. delisted)
    earnings_upcoming = cfg.get("earnings_upcoming", False)  # D-07 forward calendar (bool)
    earnings_days = cfg.get("earnings_calendar_days", 90)    # horizon for the forward calendar
    market_from = cfg.get("market_from", cfg.get("from"))  # HMM/Q-019 want market history back to 2000
    incremental = cfg.get("incremental", True)

    unknown = [d for d in datasets if d not in VALID_DATASETS]
    if unknown:
        print(f"FATAL: unknown dataset(s) {unknown}; valid: {sorted(VALID_DATASETS)}", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    results = []

    def record_series(dataset, symbol, out, low_fetch, default_from, incr_key):
        """Full refetch, or — for append-only streams when incremental — fetch the delta and merge."""
        entry = {"symbol": symbol, "dataset": dataset, "ok": False, "count": 0, "added": 0, "error": None}
        try:
            existing = _read_existing(out) if (incremental and incr_key) else []
            if existing:
                since = _latest_date(existing) or default_from
                merged, added = _merge(existing, low_fetch(symbol, since), incr_key)
                mode = f"incr≥{since}"
            else:
                new = low_fetch(symbol, default_from)
                merged, added = _merge([], new, incr_key) if incr_key else (new, len(new))
                mode = f"full≥{default_from}"
            _write(out, merged)
            entry.update(ok=True, count=len(merged), added=added)
            log(f"OK   {dataset:<18} {symbol}: {len(merged)} (+{added}) [{mode}] -> {out}")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"FAIL {dataset:<18} {symbol}: {entry['error']}")
        results.append(entry)

    def record_snapshot(dataset, symbol, out, fetch, count_fn=None):
        entry = {"symbol": symbol, "dataset": dataset, "ok": False, "count": 0, "added": 0, "error": None}
        try:
            data = fetch()
            _write(out, data)
            c = count_fn(data) if count_fn else (len(data) if isinstance(data, (list, dict)) else 0)
            entry.update(ok=True, count=c, added=c)
            log(f"OK   {dataset:<18} {symbol}: {c} [snapshot] -> {out}")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"FAIL {dataset:<18} {symbol}: {entry['error']}")
        results.append(entry)

    # 1) per-equity datasets
    for ticker in stocks:
        symbol = f"{ticker}.{exchange}"
        for ds in datasets:
            if ds in SERIES:
                low, subdir, from_key, incr_key = SERIES[ds]
                out = os.path.join(DATA_DIR, subdir, f"{ticker}.json") if subdir else os.path.join(DATA_DIR, f"{ticker}.json")
                record_series(ds, symbol, out, low, cfg.get(from_key, cfg.get("from")), incr_key)
            else:
                fetch, subdir, count_fn = SNAPSHOT[ds]
                out = os.path.join(DATA_DIR, subdir, f"{ticker}.json")
                record_snapshot(ds, symbol, out, lambda fetch=fetch, symbol=symbol: fetch(symbol), count_fn)

    # 2) market-level price series (SPY.US) — full refetch (adjusted_close is retroactively revised)
    for sym in market:
        out = os.path.join(DATA_DIR, "market", f"{sym}.json")
        record_series("market", sym, out, _fetch_eod, market_from, None)

    # 2b) market-level dividends (SPY.US) for the D-09 total-return build
    for sym in market_dividends:
        out = os.path.join(DATA_DIR, "market", "dividends", f"{sym}.json")
        record_series("market_dividends", sym, out, _fetch_dividends, market_from, None)

    # 3) survivorship-free index membership (D-15 snapshot)
    for idx in index_constituents:
        out = os.path.join(DATA_DIR, "universe", f"{idx}.json")
        record_snapshot("index_constituents", idx, out, lambda idx=idx: fetch_index_constituents(idx),
                        count_fn=lambda d: len(d.get("Components") or {}))

    # 4) exchange trading calendar / holidays (D-11 EODHD cross-check snapshot)
    for code in exchanges:
        out = os.path.join(DATA_DIR, "calendar", f"{code}.json")
        record_snapshot("exchange_calendar", code, out, lambda code=code: fetch_exchange_details(code),
                        count_fn=lambda d: len(d.get("ExchangeHolidays") or {}))

    # 5) full symbol inventory incl. delisted (D-13 snapshot)
    for code in symbol_lists:
        out = os.path.join(DATA_DIR, "symbols", f"{code}.json")
        record_snapshot("symbol_list", code, out, lambda code=code: fetch_symbol_list(code))

    # 6) forward earnings calendar for the universe (D-07 upcoming snapshot)
    if earnings_upcoming and stocks:
        symbols = [f"{t}.{exchange}" for t in stocks]
        today = datetime.now(timezone.utc).date()
        frm = today.isoformat()
        to = (today + timedelta(days=earnings_days)).isoformat()
        out = os.path.join(DATA_DIR, "earnings", "upcoming.json")
        record_snapshot("earnings_upcoming", f"{len(symbols)} symbols {frm}..{to}", out,
                        lambda: fetch_earnings_upcoming(symbols, frm, to))

    # 7) D-01 whole-exchange bulk backfill (survivorship-bias-free; INCLUDES delisted tickers).
    #    Date-partitioned: one file per trading day = every ticker. The volume persists, so each run
    #    resumes by SKIPPING day-files already present, newest-first, bounded by max_days_per_run to
    #    stay under the 100k-credit/day cap — a cold 2000→now backfill completes over several runs.
    bulk = cfg.get("eod_bulk") or {}
    if bulk.get("enabled"):
        bex = bulk.get("exchange", exchange)
        bstart = datetime.strptime(bulk.get("from") or cfg.get("from") or "2000-01-01", "%Y-%m-%d").date()
        bend = (datetime.strptime(bulk["to"], "%Y-%m-%d").date() if bulk.get("to")
                else datetime.now(timezone.utc).date())
        max_days = int(bulk.get("max_days_per_run", 500))
        today_utc = datetime.now(timezone.utc).date()
        entry = {"symbol": f"{bex} bulk {bstart}..{bend}", "dataset": "eod_bulk",
                 "ok": True, "count": 0, "added": 0, "error": None}
        fetched = skipped = consec_fail = 0
        more = False
        d = bend
        while d >= bstart:
            if d.weekday() < 5:  # Mon-Fri only; weekends are non-sessions (no call, no credit spent)
                out = os.path.join(DATA_DIR, "eod_bulk", bex, f"{d.isoformat()}.json")
                if os.path.exists(out):
                    skipped += 1
                elif fetched >= max_days:
                    more = True
                    break  # per-run budget spent; a later run resumes from bend, skipping what's done
                else:
                    try:
                        rows = _fetch_eod_bulk_day(bex, d.isoformat())
                        consec_fail = 0
                        if rows:
                            _write(out, rows); fetched += 1
                        elif (today_utc - d).days > 5:
                            _write(out, rows); fetched += 1  # confirmed holiday — cache [] so we never re-probe
                        # else: recent empty day (not yet posted / just-closed) — leave unwritten, retry next run
                    except Exception as e:
                        consec_fail += 1
                        more = True
                        if consec_fail >= 3:
                            entry["error"] = (f"stopped after 3 consecutive failures "
                                              f"(likely 100k/day credit cap): {type(e).__name__}: {e}")
                            log(f"WARN eod_bulk {bex}: {entry['error']} — re-run to resume")
                            break
            d -= timedelta(days=1)
        entry.update(count=fetched, added=fetched)
        # Bulk is a long-horizon, resumable backfill: it stays NON-FATAL (ok=True) so a run capped
        # after the per-ticker pass isn't marked FAILED for merely deferring backfill work. A genuine
        # auth/endpoint break would already fail the per-ticker jobs (they run first); the `error`
        # field + WARN log keep a stalled backfill visible in the manifest.
        results.append(entry)
        status = f"~{d.isoformat()}+ remaining, re-run to continue" if more else "complete"
        log(f"OK   {'eod_bulk':<18} {bex}: +{fetched} day-files (skipped {skipped} existing; {status})"
            f" -> {os.path.join(DATA_DIR, 'eod_bulk', bex)}/")

    all_ok = bool(results) and all(r["ok"] for r in results)
    manifest = {
        "vendor": "EODHD",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "exchange": exchange,
        "from": cfg.get("from"),
        "news_from": cfg.get("news_from"),
        "market_from": market_from,
        "incremental": incremental,
        "datasets": datasets,
        "market": market,
        "market_dividends": market_dividends,
        "index_constituents": index_constituents,
        "exchanges": exchanges,
        "symbol_lists": symbol_lists,
        "earnings_upcoming": bool(earnings_upcoming),
        "eod_bulk": cfg.get("eod_bulk") or {"enabled": False},
        "ok": all_ok,
        "results": results,
    }
    with open(os.path.join(DATA_DIR, "_run.json"), "w") as f:
        json.dump(manifest, f, indent=2)

    if not all_ok:
        # Errors are ALWAYS logged, regardless of STORE_LOGS.
        log(f"FAILED — error log: {_persist_log('error', manifest)}")
        return 1
    if STORE_LOGS:
        log(f"run log: {_persist_log('run', manifest)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Last-resort: persist the traceback to the volume so it survives termination.
        # ALWAYS written, regardless of STORE_LOGS.
        try:
            _LOG_LINES.append(traceback.format_exc())
            _persist_log("crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
