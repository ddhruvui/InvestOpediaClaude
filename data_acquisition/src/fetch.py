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
# News is walked in descending date WINDOWS (see _fetch_news): each window is up to
# MAX_PAGES * PAGE_SIZE rows, and a truncated window re-enters with to=<oldest row seen>.
# 40 windows x 100k rows is far beyond any real ticker's multi-year archive.
MAX_NEWS_WINDOWS = int(os.environ.get("MAX_NEWS_WINDOWS", "40"))
CAL_CHUNK = 100    # symbols per calendar/earnings call — keeps the URL well under any length limit
# D-11 session list written by fetch_calendar.py onto the same volume. Optional: when present,
# eod_bulk probes ONLY real NYSE sessions. Without it we fall back to Mon-Fri, which is what put
# ten holiday day-files (Labor Day, Thanksgiving, …) full of OTC/foreign rows on the volume —
# EODHD answers on market holidays, so "the exchange was shut" is not something the price feed
# can tell you. Missing file => fall back silently; the volume is cold on the very first run.
SESSIONS_PATH = os.environ.get("SESSIONS_PATH", "/workspace/data_calendar/XNYS.json")
# eod-bulk-last-day publishes the whole session at once ~15 min after the 20:00Z close; asked for
# an in-progress date it returns 0 rows (verified live 2026-08-11 15:31Z). Belt and braces: never
# even ask for TODAY before this UTC hour, so a vendor that starts serving a partial file cannot
# get one frozen into the volume — the resume logic treats any existing day-file as final forever.
BULK_TODAY_CUTOFF_UTC_HOUR = int(os.environ.get("BULK_TODAY_CUTOFF_UTC_HOUR", "21"))
# A session whose file comes back with far fewer rows than a normal session is a partial
# publication, not a quiet day. Don't freeze it; leave it for the next run.
BULK_MIN_ROWS_FRAC = float(os.environ.get("BULK_MIN_ROWS_FRAC", "0.5"))
# SETTLING WINDOW. A bulk day-file is NOT final on the evening of its session — EODHD keeps
# revising it for days. Measured 2026-08-12 on the 2026-08-10 file: pulled ~19 h after the close it
# had 44,767 rows; 34 h later the same date returned 50,368 (+5,601 codes, 5,563 of them ordinary
# equity tickers) and 4,744 changed closes. For the 503-name universe the CLOSE damage is trivial
# (5 sub-penny roundings) but VOLUME was revised on 458 of 502 names — understated in 98% of cases,
# median 4.5%, p95 38%, max 67%, and 9% low in aggregate.
#
# That matters: volume drives F6 turnover and the blueprint's default
# universe.mode = top1000_dollar_volume, so freezing evening volumes biases the universe itself.
# A day-file is therefore re-pulled when it was WRITTEN within this many days of its own session
# date. Keying on (mtime - session_date) rather than (today - session_date) also self-heals files
# frozen by earlier runs, and costs nothing once a file has been fetched after it settled.
BULK_RESETTLE_DAYS = int(os.environ.get("BULK_RESETTLE_DAYS", "5"))
# Vendor 5xx blips run minutes, not the ~6s get_json's in-request retries cover — so first-pass
# failures get one more attempt at end of run, after this pause (seconds; env-overridable).
RETRY_SWEEP_DELAY = int(os.environ.get("RETRY_SWEEP_DELAY", "60"))
# Env-gated: when truthy, a run log is stored on success. Errors/crashes log regardless (see below).
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

_ctx = None  # default = verified TLS; falls back to unverified if CA bundle is missing
_LOG_LINES = []  # captured stdout, persisted to logs/ on success (if STORE_LOGS) or always on failure
_FUND_CACHE = {}  # {symbol: fundamentals object} — one entry; shared by `fundamentals`+`estimates`


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
            # Downgrade ONLY on a certificate-VERIFICATION failure (an image with no CA bundle),
            # never on a transient ssl.SSLError such as SSLEOFError or "bad record mac" — those
            # are common on long news pagination, and treating them the same would turn one flaky
            # frame into an unverified channel carrying api_token for the rest of the run.
            # Logged via log() so the downgrade reaches the run log and the manifest.
            if _ctx is None and isinstance(err, ssl.SSLCertVerificationError):
                log("WARN: TLS certificate verification failed (no usable CA bundle) — "
                    "retrying without verification for the rest of this run")
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


def _fetch_bulk_actions_day(exchange, date, kind):
    """D-02/D-03 WHOLE-MARKET corporate actions for one session (~100 credits each).

    The per-ticker splits/div pulls only ever cover the configured 503 names, while eod_bulk covers
    ~50k tickers including delisted ones — so a universe computed from the bulk prices
    (universe.mode = top1000_dollar_volume) had no split or dividend data for anything outside the
    503. These day-files close that: every split/dividend the whole exchange printed that session.
    Row shapes differ from the per-ticker feeds: splits give {code, date, split}, dividends give
    {code, date, dividend, unadjustedValue, currency, declarationDate, recordDate, paymentDate,
    period}."""
    params = {"api_token": TOKEN, "fmt": "json", "date": date, "type": kind}
    payload = _get(f"eod-bulk-last-day/{exchange}", params)
    if not isinstance(payload, list):
        raise RuntimeError(f"eod_bulk_actions[{kind}]: expected list")
    return payload


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
    #
    # Served from the per-ticker fundamentals cache. `filter=Earnings::Trend` narrows the RESPONSE,
    # not the PRICE: it is a fundamentals-class call and costs the full 10 credits (spec §4). Since
    # config/tickers.json runs both `fundamentals` and `estimates` over the same 503 names, calling
    # it separately billed 10,060 credits/run where 5,030 buys the identical bytes — verified live:
    # fundamentals/AAPL.US?filter=Earnings::Trend == fundamentals/AAPL.US -> ["Earnings"]["Trend"].
    payload = (_fundamentals(symbol).get("Earnings") or {}).get("Trend")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return [{"date": stamp, "trend": payload}]


def _fetch_news(symbol, from_date, to_date=None):
    """D-08: timestamped headlines, newest-first. Depth is ~Dec-2020 onward (API launch).

    WINDOWED, not one flat offset run. EODHD returns newest-first, so a plain offset walk that
    hits the MAX_PAGES * PAGE_SIZE ceiling (100,000 rows) drops the OLDEST articles in the window
    — and the next run's watermark is max(stored date), so it asks `from=<newest>` and never goes
    back for them. That hole is permanent and, worse, invisible.

    This is not hypothetical headroom: NVDA already returns 71,619 rows for a ONE-year window
    (MSFT 39,990, AAPL 37,290). Widening to the spec's multi-year backfill puts the busiest names
    several times over the ceiling. So when a window truncates we re-enter with `to=<oldest row
    fetched>` and keep walking backwards until a window comes back short."""
    out, seen = [], set()
    for window in range(1, MAX_NEWS_WINDOWS + 1):
        base = {"api_token": TOKEN, "fmt": "json", "s": symbol}
        if from_date:
            base["from"] = from_date
        if to_date:
            base["to"] = to_date
        rows, truncated = _paginate("news", base, label=f"{symbol} w{window}")
        fresh = 0
        oldest = None
        for r in rows:
            k = _news_key(r)
            if k not in seen:
                seen.add(k)
                out.append(r)
                fresh += 1
            d = (r.get("date") or "")[:10]
            if d and (oldest is None or d < oldest):
                oldest = d
        if not truncated:
            return out
        # Truncated: step the window back to the oldest row we actually received.
        if oldest is None or oldest == to_date or fresh == 0:
            # No progress — a single day exceeds the ceiling, or the vendor ignored `to`.
            log(f"WARN news {symbol}: window stalled at to={to_date} after {len(out)} rows "
                f"— older articles are NOT retrievable by this method")
            return out
        to_date = oldest
    log(f"WARN news {symbol}: hit MAX_NEWS_WINDOWS ({MAX_NEWS_WINDOWS}) at {len(out)} rows "
        f"(oldest reached {to_date}) — older articles may be truncated")
    return out


# --- snapshot fetchers (whole objects, always refetched) ----------------------------------------

def _fundamentals(symbol):
    """The 10-credit fundamentals object for `symbol`, fetched at most ONCE per ticker.

    Cached rather than re-derived from disk on purpose: reading DATA_DIR/fundamentals/<T>.json
    would couple `estimates` to a prior successful snapshot write and could serve a stale
    prior-run object into today's PIT snapshot. The cache is keyed by symbol and cleared as the
    per-ticker loop advances, so whichever of {fundamentals, estimates} runs first pays, the other
    is free, and the order in `datasets` stops mattering."""
    if symbol not in _FUND_CACHE:
        payload = _get(f"fundamentals/{symbol}", {"api_token": TOKEN})
        if not isinstance(payload, dict):
            raise RuntimeError("fundamentals: expected object")
        _FUND_CACHE.clear()          # one ticker in flight at a time — bound the 950 KB objects
        _FUND_CACHE[symbol] = payload
    return _FUND_CACHE[symbol]


def fetch_fundamentals(symbol):
    # D-05/06 cross-check + D-07 Earnings::History. Stored lossless: the full nested object
    # (General/Highlights/Valuation/SharesStats/Earnings/Financials/...). Downstream PIT extraction
    # selects the blocks it needs; keeping it whole avoids silently dropping a field a model wants.
    return _fundamentals(symbol)


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
# dataset -> (low_level_fetch, subdir, from_cfg_key, incremental_key_fn, supports_backfill).
# supports_backfill: the vendor can serve an OLDER window on request, so widening the config's
# start date re-pulls the gap. False for `estimates`, whose history is our own pull log — there is
# no earlier snapshot to go and get.
SERIES = {
    "eod":       (_fetch_eod,       None,         "from",      None,            False),
    "dividends": (_fetch_dividends, "dividends",  "from",      None,            False),
    "splits":    (_fetch_splits,    "splits",     "from",      None,            False),
    "estimates": (_fetch_estimates, "estimates",  "from",      _estimates_key,  False),
    "news":      (_fetch_news,      "news",       "news_from", _news_key,       True),
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


def _oldest_date(rows):
    dates = [(r.get("date") or "")[:10] for r in rows if r.get("date")]
    return min(dates) if dates else None


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


def _write(out_path, data, indent=None):
    """Atomically write JSON: dump to a sibling .part, flush+fsync, then os.replace() into place.
    A pod killed mid-write can then never leave a truncated file — critical for eod_bulk, whose
    resume logic treats any existing day-file as complete and skips it forever."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def _load_sessions():
    """D-11 session set from the volume, or None when fetch_calendar.py hasn't run yet."""
    try:
        with open(SESSIONS_PATH) as f:
            payload = json.load(f)
        sessions = set(payload.get("sessions") or [])
        if sessions:
            log(f"     eod_bulk: using D-11 session calendar {payload.get('calendar')} "
                f"({len(sessions)} sessions, exchange_calendars {payload.get('package_version')})")
            return sessions
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    log(f"     eod_bulk: no D-11 calendar at {SESSIONS_PATH} — falling back to Mon-Fri "
        f"(market holidays will be probed and may store non-session files)")
    return None


def _bulk_unsettled(path, session_date):
    """True if this day-file was written before its session had settled, so it must be re-pulled.

    Compares the file's MTIME against its own session date (not against today): a file fetched
    2 days after its session is suspect forever until re-fetched later, which is what lets this
    repair day-files frozen by earlier runs instead of only protecting future ones."""
    if BULK_RESETTLE_DAYS <= 0:
        return False
    try:
        written = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).date()
    except OSError:
        return False
    return (written - session_date).days < BULK_RESETTLE_DAYS


def _bulk_row_floor(bulk_dir):
    """Median row count of already-stored SESSION day-files, times BULK_MIN_ROWS_FRAC.

    Used to reject a partial publication. Returns 0 (no floor) until enough files exist to make a
    median meaningful — a cold backfill must not refuse its own first days."""
    try:
        names = [n for n in os.listdir(bulk_dir) if n.endswith(".json")]
    except OSError:
        return 0
    counts = []
    for n in sorted(names, reverse=True)[:20]:
        try:
            with open(os.path.join(bulk_dir, n)) as f:
                rows = json.load(f)
            if isinstance(rows, list) and len(rows) > 1000:  # skip cached holiday/empty files
                counts.append(len(rows))
        except (OSError, ValueError):
            continue
    if len(counts) < 3:
        return 0
    counts.sort()
    return int(counts[len(counts) // 2] * BULK_MIN_ROWS_FRAC)


def _credit_budget():
    """(used, cap) from EODHD's own counter, or (None, None) if unavailable.

    The /user call is itself free. The counter resets lazily on the first request of a new GMT day,
    so `apiRequestsDate` is authoritative for *which* day the number belongs to."""
    try:
        d = _get("user", {"api_token": TOKEN, "fmt": "json"})
        return int(d["apiRequests"]), int(d["dailyRateLimit"])
    except Exception:
        return None, None


def main():
    if not TOKEN:
        print("FATAL: EODHD_API_TOKEN not set", file=sys.stderr)
        return 1

    # CREDIT PREFLIGHT. Running twice in one GMT day is the failure mode this prevents: the second
    # run starts with the remainder, gets a few tickers in, and then every remaining job 402s —
    # 2,993 of 3,026 jobs "failed" that way on 2026-08-14 while changing no data at all, because a
    # failed job simply does not write. That is a alarming-looking manifest for a no-op. Bail early
    # and cleanly instead, so a same-day re-run is a cheap skip rather than a fake catastrophe.
    used, cap = _credit_budget()
    if used is not None and cap:
        left = cap - used
        need = int(os.environ.get("MIN_CREDITS", "20000"))
        log(f"     credits: {used:,}/{cap:,} used, {left:,} left (need >= {need:,} to start)")
        if left < need:
            log(f"SKIP run: only {left:,} credits remain today — a full pass needs ~{need:,}. "
                f"The counter resets at midnight GMT; re-run then. Nothing was fetched.")
            return 0
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
    retry_queue = []  # (index into results, attempt fn) per first-pass failure — end-of-run sweep

    def record_series(dataset, symbol, out, low_fetch, default_from, incr_key, backfill=False):
        """Full refetch, or — for append-only streams when incremental — fetch the delta and merge."""
        def attempt():
            entry = {"symbol": symbol, "dataset": dataset, "ok": False, "count": 0, "added": 0, "error": None}
            try:
                existing = _read_existing(out) if (incremental and incr_key) else []
                if existing:
                    since = _latest_date(existing) or default_from
                    new = low_fetch(symbol, since)
                    # WIDENING THE WINDOW MUST ACTUALLY WIDEN IT. The tail watermark only ever
                    # moves forward, so if the config's start date is now EARLIER than the oldest
                    # row on disk, an incremental run would fetch only the new tail and silently
                    # ignore the extra history the operator just asked for — the config changes and
                    # nothing happens. Detect that gap and pull it too.
                    oldest = _oldest_date(existing)
                    mode = f"incr≥{since}"
                    if backfill and default_from and oldest and default_from < oldest:
                        new += low_fetch(symbol, default_from, to_date=oldest)
                        mode = f"incr≥{since} +backfill {default_from}..{oldest}"
                    merged, added = _merge(existing, new, incr_key)
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
            return entry
        entry = attempt()
        if not entry["ok"]:
            retry_queue.append((len(results), attempt))
        results.append(entry)

    def record_snapshot(dataset, symbol, out, fetch, count_fn=None):
        def attempt():
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
            return entry
        entry = attempt()
        if not entry["ok"]:
            retry_queue.append((len(results), attempt))
        results.append(entry)

    # 1) per-equity datasets
    for ticker in stocks:
        symbol = f"{ticker}.{exchange}"
        for ds in datasets:
            if ds in SERIES:
                low, subdir, from_key, incr_key, backfill = SERIES[ds]
                out = os.path.join(DATA_DIR, subdir, f"{ticker}.json") if subdir else os.path.join(DATA_DIR, f"{ticker}.json")
                record_series(ds, symbol, out, low, cfg.get(from_key, cfg.get("from")), incr_key,
                              backfill)
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

    # 6) forward earnings calendar for the universe (D-07 upcoming).
    #    APPEND-ONLY, one dated file per pull (M1-01): F8's `days_to_earnings` at time t must use
    #    the calendar AS KNOWN AT t. A single overwritten upcoming.json only ever holds today's
    #    view, so a backtest reading it would see announcement dates that were published after t —
    #    look-ahead, and the silent kind. upcoming.json is still written as a "latest" convenience
    #    copy; the dated files under earnings/upcoming/ are the point-in-time record.
    if earnings_upcoming and stocks:
        symbols = [f"{t}.{exchange}" for t in stocks]
        today = datetime.now(timezone.utc).date()
        frm = today.isoformat()
        to = (today + timedelta(days=earnings_days)).isoformat()
        dated = os.path.join(DATA_DIR, "earnings", "upcoming", f"{frm}.json")
        latest = os.path.join(DATA_DIR, "earnings", "upcoming.json")

        def _fetch_upcoming():
            rows = fetch_earnings_upcoming(symbols, frm, to)
            payload = {"pulled_at_utc": datetime.now(timezone.utc).isoformat(),
                       "vendor": "EODHD", "source_endpoint": f"{API}/calendar/earnings",
                       "spec_item": "D-07", "from": frm, "to": to,
                       "n_symbols": len(symbols), "earnings": rows}
            _write(latest, payload)
            return payload

        record_snapshot("earnings_upcoming", f"{len(symbols)} symbols {frm}..{to}", dated,
                        _fetch_upcoming, count_fn=lambda d: len(d.get("earnings") or []))

    # 7) D-01 whole-exchange bulk backfill (survivorship-bias-free; INCLUDES delisted tickers).
    #    Date-partitioned: one file per trading day = every ticker. The volume persists, so each run
    #    resumes by SKIPPING day-files already present, newest-first, bounded by max_days_per_run to
    #    stay under the 100k-credit/day cap — a cold 2000→now backfill completes over several runs.
    # Shared by both bulk blocks; hoisted so eod_bulk_actions works with eod_bulk disabled.
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    bulk = cfg.get("eod_bulk") or {}
    if bulk.get("enabled"):
        bex = bulk.get("exchange", exchange)
        bstart = datetime.strptime(bulk.get("from") or cfg.get("from") or "2000-01-01", "%Y-%m-%d").date()
        bend = (datetime.strptime(bulk["to"], "%Y-%m-%d").date() if bulk.get("to")
                else datetime.now(timezone.utc).date())
        max_days = int(bulk.get("max_days_per_run", 500))
        entry = {"symbol": f"{bex} bulk {bstart}..{bend}", "dataset": "eod_bulk",
                 "ok": True, "count": 0, "added": 0, "error": None}
        bulk_dir = os.path.join(DATA_DIR, "eod_bulk", bex)
        sessions = _load_sessions()
        row_floor = _bulk_row_floor(bulk_dir)
        fetched = skipped = consec_fail = nonsession = deferred = 0
        more = False
        d = bend
        while d >= bstart:
            iso = d.isoformat()
            # Real NYSE sessions only when the D-11 calendar is available; Mon-Fri otherwise.
            is_session = (iso in sessions) if sessions is not None else (d.weekday() < 5)
            if is_session:
                out = os.path.join(bulk_dir, f"{iso}.json")
                if os.path.exists(out) and not _bulk_unsettled(out, d):
                    skipped += 1
                elif d == today_utc and now_utc.hour < BULK_TODAY_CUTOFF_UTC_HOUR:
                    # Today's session hasn't settled — don't spend the credit, don't risk freezing
                    # a partial file. Tomorrow's run (or a post-cutoff re-run) picks it up.
                    deferred += 1
                    more = True
                elif fetched >= max_days:
                    more = True
                    break  # per-run budget spent; a later run resumes from bend, skipping what's done
                else:
                    try:
                        rows = _fetch_eod_bulk_day(bex, iso)
                        consec_fail = 0
                        if rows and row_floor and len(rows) < row_floor:
                            # Partial publication: a session file this short is not a quiet day.
                            # Freezing it would be permanent (resume skips any existing file).
                            log(f"WARN eod_bulk {bex} {iso}: only {len(rows)} rows "
                                f"(< floor {row_floor}) — looks partial, not storing; retry next run")
                            deferred += 1
                            more = True
                        elif rows:
                            _write(out, rows); fetched += 1
                        elif (today_utc - d).days > 5:
                            _write(out, rows); fetched += 1  # settled no-data day — cache [] so we never re-probe
                        else:
                            deferred += 1
                            more = True  # recent empty day (not yet posted) — retry next run
                    except Exception as e:
                        consec_fail += 1
                        more = True
                        if consec_fail >= 3:
                            entry["error"] = (f"stopped after 3 consecutive failures "
                                              f"(likely 100k/day credit cap): {type(e).__name__}: {e}")
                            log(f"WARN eod_bulk {bex}: {entry['error']} — re-run to resume")
                            break
            elif sessions is not None and d.weekday() < 5:
                nonsession += 1  # weekday the exchange was shut — no call, no credit, no phantom file
            d -= timedelta(days=1)
        entry.update(count=fetched, added=fetched, deferred=deferred,
                     non_sessions_skipped=nonsession, row_floor=row_floor)
        # Bulk is a long-horizon, resumable backfill, so merely DEFERRING work (per-run cap spent,
        # today not settled, a recent day not yet published) stays NON-FATAL — a run must not be
        # marked FAILED for pacing itself. But a run that BAILED on consecutive errors is a real
        # break (bulk not activated on the plan — §8-4 — a revoked token, or the 100k/day cap) and
        # must fail the run: leaving it ok=True meant a completely dead bulk endpoint reported
        # success every day while the price backfill silently stopped advancing.
        # ...but only when the run made NO progress. Hitting the 100k/day cap partway through a
        # multi-day backfill sets the same `error` and is entirely normal — failing the run every
        # day for the duration of a planned backfill would train you to ignore the flag. Zero
        # day-files plus consecutive errors is the shape of a genuine break.
        if entry["error"] and fetched == 0:
            entry["ok"] = False
        results.append(entry)
        status = f"~{d.isoformat()}+ remaining, re-run to continue" if more else "complete"
        log(f"OK   {'eod_bulk':<18} {bex}: +{fetched} day-files (skipped {skipped} existing, "
            f"{nonsession} non-sessions, {deferred} deferred; {status}) -> {bulk_dir}/")

    # 7b) D-02/D-03 whole-market corporate actions, date-partitioned like eod_bulk.
    #     Separate `from` because the credit profile is different: 200/session (splits+dividends)
    #     on top of eod_bulk's 100, so a full 5-year backfill is ~253k credits / 3 days of cap.
    #     Defaults to forward-only from `from` — the per-ticker feeds already cover the 503 names
    #     historically; what was missing is the rest of the bulk universe. Widen deliberately.
    bulk_act = cfg.get("eod_bulk_actions") or {}
    if bulk_act.get("enabled"):
        bex = bulk_act.get("exchange", exchange)
        astart = datetime.strptime(bulk_act.get("from") or cfg.get("from") or "2000-01-01",
                                   "%Y-%m-%d").date()
        aend = (datetime.strptime(bulk_act["to"], "%Y-%m-%d").date() if bulk_act.get("to")
                else datetime.now(timezone.utc).date())
        amax = int(bulk_act.get("max_days_per_run", 60))
        kinds = bulk_act.get("types") or ["splits", "dividends"]
        sessions_a = _load_sessions()
        entry = {"symbol": f"{bex} bulk-actions {astart}..{aend}", "dataset": "eod_bulk_actions",
                 "ok": True, "count": 0, "added": 0, "error": None}
        got = askip = adefer = 0
        d = aend
        while d >= astart and got < amax:
            iso = d.isoformat()
            is_sess = (iso in sessions_a) if sessions_a is not None else (d.weekday() < 5)
            if is_sess:
                if d == today_utc and now_utc.hour < BULK_TODAY_CUTOFF_UTC_HOUR:
                    adefer += 1
                else:
                    for kind in kinds:
                        out = os.path.join(DATA_DIR, "eod_bulk_actions", bex, kind, f"{iso}.json")
                        if os.path.exists(out):
                            askip += 1
                            continue
                        try:
                            rows = _fetch_bulk_actions_day(bex, iso, kind)
                            # An action-free session is a real answer, so [] is cached — unlike
                            # eod_bulk, where an empty price file means "not published yet".
                            _write(out, rows)
                            got += 1
                        except Exception as e:
                            entry["error"] = f"{type(e).__name__}: {e}"
                            log(f"WARN eod_bulk_actions {kind} {iso}: {entry['error']}")
                            got = amax
                            break
            d -= timedelta(days=1)
        entry.update(count=got, added=got, deferred=adefer)
        if entry["error"] and got == 0:
            entry["ok"] = False
        results.append(entry)
        log(f"OK   {'eod_bulk_actions':<18} {bex}: +{got} day-file(s) over {kinds} "
            f"(skipped {askip} existing, {adefer} deferred) -> "
            f"{os.path.join(DATA_DIR, 'eod_bulk_actions', bex)}/")

    # End-of-run retry sweep: each first-pass failure gets one fresh attempt before the run is
    # judged. record_series/record_snapshot are idempotent (atomic writes, incremental re-reads
    # disk), so a re-run is safe; eod_bulk is excluded — it is non-fatal and resumes across runs.
    if retry_queue:
        log(f"RETRY {len(retry_queue)} failed task(s) after {RETRY_SWEEP_DELAY}s pause ...")
        time.sleep(RETRY_SWEEP_DELAY)
        healed = 0
        for idx, attempt in retry_queue:
            entry = attempt()
            entry["retried"] = True  # manifest: distinguishes healed-on-retry from clean first pass
            healed += 1 if entry["ok"] else 0
            results[idx] = entry
        log(f"RETRY sweep healed {healed}/{len(retry_queue)}")

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
    _write(os.path.join(DATA_DIR, "_run.json"), manifest, indent=2)

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
