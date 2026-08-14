#!/usr/bin/env python3
"""Download every Sharadar dataset the v1.2 spec needs, for the universe in sharadar.json —
via the Sharadar RETAIL API at api.sharadar.com.

Pure stdlib (no pip). Covers the "Nasdaq Data Link — Sharadar US Equities bundle" column of
`../../Data Acquisition Specification — FINAL v1.2.md` (the D-items Sharadar is the source or
cross-check for). `scripts/launch.sh nasdaq` runs it (EODHD is the sibling `fetch.py`).

NOTE ON THE ENDPOINT: retail subscriptions bought on sharadar.com are served from
**https://api.sharadar.com/v1.0/data/<endpoint>** (NOT the institutional Nasdaq Data Link datatables
API at data.nasdaq.com — a sharadar.com key is anonymous there and gets IP-throttled). The two APIs
share Sharadar's schema + filter operators but differ in host, endpoint NAMES, response shape, and
paging. This fetcher targets the sharadar.com retail API.

    Spec item                     Sharadar table   api endpoint  -> output
    D-12 second-vendor prices     SEP              stocks        -> DATA_DIR/SEP/<TICKER>.json
    D-05/06 PIT fundamentals      SF1 (ARQ)        fundamentals  -> DATA_DIR/SF1/<TICKER>.json
    D-04 corporate actions        ACTIONS          actions       -> DATA_DIR/ACTIONS/<TICKER>.json
    D-13 entity master            TICKERS          tickers       -> DATA_DIR/TICKERS/SHARADAR.json
    D-15 index constituents       SP500            sp500         -> DATA_DIR/SP500/SHARADAR.json

API CONTRACT (verified live against api.sharadar.com):
  - Request : GET https://api.sharadar.com/v1.0/data/<endpoint>?api_key=..&format=json&limit=..&<filters>
  - Response: {"count": N, "data": [ {row-dict}, ... ]}   (rows are already dicts — no columns/zip)
  - Filters : ticker=, dimension= (SF1), and range operators <col>.gte= / <col>.lte= — same operator
              syntax as the datatables API. Incremental uses lastupdated.gte= (SEP/SF1/TICKERS) or
              date.gte= (ACTIONS/SP500 carry no lastupdated).
  - Paging  : OFFSET-based. The server caps ANY result set at ROW_CAP (100,000) rows regardless of
              the `limit` asked for, so every call is paged: offset=0, ROW_CAP, 2*ROW_CAP, … until a
              page comes back short. Verified live 2026-08-11 (offset=100000 returns a different row
              set than offset=0). Never trust `count` alone — a bare count==100000 is a CAP HIT.
  - Ticker  : the `ticker` comma-list is capped at **30 tickers AND 200 characters** (server-enforced,
              HTTP 400 "Too many tickers" / "ticker exceeds maximum length of 200 characters").
              _ticker_batches() packs to BOTH limits.
  - Limits  : exposed via **x-**ratelimit-limit / -remaining / -reset (reset is a UNIX TIMESTAMP, not
              a delay) plus a weighted budget (x-ratelimit-weighted-limit/-remaining; a full-table
              call costs 100). We burst until nearly exhausted, then sleep to the reset (see _pace).

PER-TICKER SERIES (config "tables", applied to each "stocks" entry):
    SEP      D-12  ticker,date,open,high,low,close,volume,closeadj,closeunadj,lastupdated.
             closeunadj = RAW unadjusted close (immutable source of truth); closeadj = FULLY adjusted;
             close = split/stock-div adjusted only. Cross-checks EODHD D-01 (>25 bps => quarantine).
    SF1      D-05/06  dimension=ARQ (as-reported quarterly — the PIT feature source, T-11). On the
             retail API the SEC-filing date is the `date` column (the datatables API calls it
             `datekey`); M2-03 adds the +1-session visibility lag off it downstream. calendardate =
             normalized period-end; reportperiod = as-reported period-end. COGS is the `cor` field.
    ACTIONS  D-04  date,action,ticker,name,value,contraticker,contraname (no lastupdated column).

WHOLE-TABLE SNAPSHOTS (config "whole_tables", no ticker filter — survivorship-bias-free):
    TICKERS  D-13  permaticker (M1 entity key), ticker,name,exchange,isdelisted,category,cusips,
             siccode,sector,industry,firstpricedate,lastpricedate,table,lastupdated. ~25k rows incl.
             delisted, returned in one high-limit call. Join symbol-change events via permaticker.
    SP500    D-15  date,action,ticker,name,contraticker,contraname,note — S&P 500 add/remove history.

INCREMENTAL (default on; "incremental": false forces a full refetch). The network volume persists
DATA_DIR between launches, so warm runs only add what's new (M1-01 append-only — restatements arrive
as NEW rows keyed by lastupdated; nothing is overwritten): SEP/SF1 pull lastupdated.gte=max-seen;
ACTIONS (no lastupdated) tops up date.gte=max-seen; TICKERS/SP500 are whole-table snapshots refetched
whole but SKIPPED on warm runs whose file is younger than whole_refresh_days.

Self-termination is bootstrap.sh's job, so this runs/tests locally:

    DATA_DIR=./data_nasdaq CONFIG_PATH=data_acquisition/config/sharadar.json \
      SHARADAR_API_KEY=xxx STORE_LOGS=true python3 data_acquisition/src/fetch_nasdaq.py

Logging (env-controlled): `_run.json` manifest is always written. A run log (`logs/run-<ts>.log`) is
stored ONLY when `STORE_LOGS` is truthy; failures (`logs/error-<ts>.log`) and crashes
(`logs/crash-<ts>.log`) are ALWAYS logged. Exit code: 0 if every job succeeded, 1 otherwise.
"""
import csv
import gzip
import http.client
import io
import json
import os
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone

# SHARADAR_API_KEY is the sharadar.com retail key; NASDAQ_DATA_LINK_API_KEY kept as a back-compat alias.
TOKEN = os.environ.get("SHARADAR_API_KEY", "") or os.environ.get("NASDAQ_DATA_LINK_API_KEY", "")
DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_nasdaq")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/sharadar.json")
API = "https://api.sharadar.com/v1.0/data"
# logical table (spec/config) -> api.sharadar.com endpoint name
ENDPOINT = {"SEP": "stocks", "SF1": "fundamentals", "ACTIONS": "actions",
            "TICKERS": "tickers", "SP500": "sp500"}
# Server-side row cap on ANY single response, measured live: asking for limit=150000 or 1000000
# both return exactly 100000 rows. Paging is by `offset`, so this doubles as our page size.
ROW_CAP = int(os.environ.get("SHARADAR_ROW_CAP", "100000"))
LIMIT = ROW_CAP           # per-page `limit` we ask for (the server will not exceed ROW_CAP anyway)
MAX_PAGES = int(os.environ.get("SHARADAR_MAX_PAGES", "200"))  # backstop: 200 * 100k = 20M rows
# Server-enforced ticker-list caps (HTTP 400 past either): at most 30 tickers AND 200 characters.
# _ticker_batches() packs to both. Keep headroom under each so a long-symbol run can't skim the edge.
MAX_TICKERS_PER_CALL = int(os.environ.get("SHARADAR_BATCH", "30"))
MAX_TICKER_CHARS = int(os.environ.get("SHARADAR_TICKER_CHARS", "195"))
# Small polite gap between calls; _pace() additionally sleeps to the window reset when the
# x-ratelimit headers say we're nearly out, so a full-universe pass never trips the limit.
PACE_SEC = float(os.environ.get("SHARADAR_PACE_SEC", "0.05"))
RL_BUFFER = 3           # start waiting for the reset when this few requests remain in the window
WHOLE_REFRESH_DAYS = 7  # skip re-pulling a whole-table snapshot whose file is younger than this
# Bulk-zip window for the survivorship-free whole-market pull. 5 is what the retail tier serves;
# years=10 returns 403.
WHOLE_MARKET_YEARS = int(os.environ.get("SHARADAR_WHOLE_YEARS", "5"))
# Vendor 5xx blips run minutes, not the seconds in-request retries cover — so first-pass failures
# get one more attempt at end of run, after this pause (seconds; env-overridable).
RETRY_SWEEP_DELAY = int(os.environ.get("RETRY_SWEEP_DELAY", "60"))
# api.sharadar.com sits behind Cloudflare, which 403s the default "Python-urllib" User-Agent as a
# bot. Any real UA passes — send a descriptive one (override via SHARADAR_USER_AGENT if needed).
USER_AGENT = os.environ.get("SHARADAR_USER_AGENT", "InvestOpediaClaude-DataAcquisition/1.0")
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

# Per-ticker series spec. date_filter = the range column for the FULL backfill window; from_key =
# config key holding that window's start; incr_col = the column used for the warm-run .gte filter.
PER_TICKER = {
    "SEP":     {"date_filter": "date",         "from_key": "from",     "incr_col": "lastupdated"},
    "SF1":     {"date_filter": "calendardate", "from_key": "sf1_from", "incr_col": "lastupdated"},
    "ACTIONS": {"date_filter": "date",         "from_key": "from",     "incr_col": "date"},
}
# Whole-table snapshots -> the filter needed to get the FULL table.
# SP500 gotcha: an UNFILTERED /sp500 call silently returns only a trailing ~1 year (2,559 rows,
# 2025-08-28 onward). Add date.gte and the same call returns 59,669 rows back to 1957-03-04.
# Nothing in the response marks it as a default window, so the short answer looks complete —
# this had truncated D-15's constituent history to one year. (Settles spec §8-3: the "1957 start"
# claim is TRUE, but only with an explicit filter.) TICKERS is an entity master with no such
# default; it returns all ~75k rows unfiltered.
WHOLE_TABLES = {"TICKERS": {}, "SP500": {"date.gte": "1957-01-01"}}

_ctx = None  # default = verified TLS; falls back to unverified if the CA bundle is missing
_LOG_LINES = []


def log(msg):
    print(msg, flush=True)
    _LOG_LINES.append(msg)


def _persist_log(kind, manifest=None):
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
    """GET url -> (status, parsed_json_or_None, headers_lower_dict, error_body). Retries transient
    errors + 429.

    On 429 we sleep to the x-ratelimit-reset instant (Sharadar's window is long, so a short backoff
    is pointless) and retry. TLS downgrades once if the CA bundle is missing (pod without certs).
    `error_body` carries the vendor's JSON error text on a 4xx — api.sharadar.com explains exactly
    which constraint was violated there ("ticker accepts at most 30 tickers per request"), and
    losing it turns a fixable 400 into an opaque one.
    """
    global _ctx
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=300, context=_ctx) as r:
                headers = {k.lower(): v for k, v in r.headers.items()}
                return r.status, json.loads(r.read().decode("utf-8")), headers, None
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            try:
                body = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                body = None
            if e.code == 429 and attempt < 6:
                attempt += 1
                wait = _reset_wait(headers)
                log(f"     429 rate-limited — sleeping {wait}s for the window reset (attempt {attempt})")
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 3:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            return e.code, None, headers, body
        except (ssl.SSLError, urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError) as e:
            err = e if isinstance(e, ssl.SSLError) else getattr(e, "reason", e)
            # Certificate-VERIFICATION failures only (image with no CA bundle) — not transient
            # SSLErrors, which would otherwise leave api_key riding an unverified channel for the
            # rest of the run. Logged via log() so it lands in the manifest, not just stdout.
            if _ctx is None and isinstance(err, ssl.SSLCertVerificationError):
                log("WARN: TLS certificate verification failed (no usable CA bundle) — "
                    "retrying without verification for the rest of this run")
                _ctx = ssl._create_unverified_context()
                continue
            if attempt < 4:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            raise


def _rl(headers, name):
    """Read a rate-limit header. The API sends them x-prefixed (x-ratelimit-remaining); the
    unprefixed spelling is accepted too so a vendor rename can't silently disable pacing."""
    for k in (f"x-ratelimit-{name}", f"ratelimit-{name}"):
        try:
            return int(headers[k])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def _reset_wait(headers):
    """Seconds to sleep until the rate-limit window resets.

    `x-ratelimit-reset` is a UNIX TIMESTAMP (e.g. 1786462532), NOT a delay — sleeping on it raw
    would park the pod for ~56 years. Anything that looks like an epoch is converted to a delay;
    a small value is taken as an already-relative delay. Always clamped to [1, 900]."""
    v = _rl(headers, "reset")
    if v is None:
        return 60
    wait = (v - time.time()) if v > 10_000_000 else v
    return int(max(1, min(900, wait + 2)))


def _pace(headers):
    """Respect the request AND weighted budgets: a small gap normally; sleep to the window reset
    when either x-ratelimit-remaining or x-ratelimit-weighted-remaining runs low. A whole-table
    call costs 100 weighted units, so the weighted budget is what actually binds on big pulls."""
    if PACE_SEC > 0:
        time.sleep(PACE_SEC)
    remaining = _rl(headers, "remaining")
    weighted = _rl(headers, "weighted-remaining")
    cost = _rl(headers, "cost") or 1
    low = (remaining is not None and remaining <= RL_BUFFER) or \
          (weighted is not None and weighted <= cost * RL_BUFFER)
    if low:
        wait = _reset_wait(headers)
        log(f"     rate-limit: {remaining} req / {weighted} weighted left — sleeping {wait}s for the reset")
        time.sleep(wait)


def _get(endpoint, params):
    """GET {API}/{endpoint}?{params}+api_key+format+limit -> parsed JSON, raising on failure.

    A 4xx carries the vendor's own explanation of which constraint was violated; surface it in the
    exception text instead of a bare status code."""
    q = dict(params, api_key=TOKEN, format="json", limit=LIMIT)
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(q, safe=',')}"  # keep the ticker comma-list literal
    status, payload, headers, body = get_json(url)
    if status in (401, 403):
        raise RuntimeError(f"{status} — Sharadar key rejected for /{endpoint} "
                           f"(is SHARADAR_API_KEY a valid api.sharadar.com key with the bundle?)")
    if status == 404:
        raise RuntimeError(f"404 — no such endpoint /{endpoint}")
    if status != 200 or payload is None:
        detail = f" — {body.strip()}" if body else ""
        raise RuntimeError(f"unexpected response (status={status}) from /{endpoint}{detail}")
    _pace(headers)
    return payload


def _rows(payload):
    """Sharadar retail payload -> list of row dicts (already keyed; no columns/data zip)."""
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _fetch_bulk_zip(endpoint, years, dimension=None):
    """`years=N` -> 302 -> CSV zip -> list of row dicts. urllib follows the redirect itself."""
    url = f"{API}/{endpoint}?{urllib.parse.urlencode({'api_key': TOKEN, 'format': 'json', 'years': years})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=1800, context=_ctx) as r:
        blob = r.read()
    if blob[:2] != b"PK":
        raise RuntimeError(f"bulk years={years} did not return a zip "
                           f"({len(blob)} bytes, starts {blob[:40]!r})")
    out = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            for row in csv.DictReader(io.TextIOWrapper(f, "utf-8")):
                if dimension and row.get("dimension") != dimension:
                    continue
                out.append(row)
    return out


def _dump_gz(path, payload):
    """Gzipped JSON — the whole-market SF1 set is ~122k rows x 112 columns; gzip keeps it ~40 MB."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def _ticker_batches(syms):
    """Split the universe into comma-lists the API will accept: <= MAX_TICKERS_PER_CALL entries AND
    <= MAX_TICKER_CHARS characters. Both caps are server-enforced (HTTP 400 past either), and the
    character cap is the one a count-only split silently walks past on long symbols."""
    batch, size = [], 0
    for s in syms:
        add = len(s) + (1 if batch else 0)
        if batch and (len(batch) >= MAX_TICKERS_PER_CALL or size + add > MAX_TICKER_CHARS):
            yield batch
            batch, size = [], 0
            add = len(s)
        batch.append(s)
        size += add
    if batch:
        yield batch


def _fetch(endpoint, params, label=None):
    """Fetch every matching row, paging by `offset` past the server's ROW_CAP.

    The server truncates any response at ROW_CAP rows however large a `limit` we ask for, so a
    single call can never be trusted to be complete. We page until a short page arrives; MAX_PAGES
    is a runaway backstop and is the ONLY condition that reports truncated=True."""
    rows = []
    for page in range(MAX_PAGES):
        payload = _get(endpoint, dict(params, offset=page * ROW_CAP) if page else params)
        got = _rows(payload)
        rows += got
        if len(got) < ROW_CAP:
            if STORE_LOGS:
                log(f"     {endpoint} {label or ''}: {len(rows)} rows"
                    + (f" ({page + 1} pages)" if page else ""))
            return rows, False
    log(f"WARN {endpoint} {label or ''}: hit MAX_PAGES ({MAX_PAGES}) — rows may be truncated")
    return rows, True


# --- append-only merge (M1-01): dedup by the WHOLE row so a restated/adjusted row is kept as new ---

def _row_key(r):
    return json.dumps(r, sort_keys=True, default=str)


def _sort_key(r):
    return str(r.get("date") or r.get("calendardate") or r.get("datekey") or r.get("lastupdated") or "")


def _merge(existing, new):
    by = {_row_key(r): r for r in existing}
    added = 0
    for r in new:
        k = _row_key(r)
        if k not in by:
            by[k] = r
            added += 1
    return sorted(by.values(), key=_sort_key), added


def _latest(rows, col):
    vals = [str(r.get(col))[:10] for r in rows if r.get(col)]
    return max(vals) if vals else None


def _earliest(rows, col):
    vals = [str(r.get(col))[:10] for r in rows if r.get(col)]
    return min(vals) if vals else None


def _window_path(table):
    return os.path.join(DATA_DIR, table, "_window.json")


def _read_window(table):
    """The `from` date that produced the data on disk, or None if never recorded."""
    try:
        with open(_window_path(table)) as f:
            v = json.load(f).get("from")
        return v if isinstance(v, str) else None
    except (FileNotFoundError, ValueError, AttributeError):
        return None


def _write_window(table, frm):
    """Record the window a FULL pull covered, so a later widening is detected exactly rather than
    guessed from row dates (see fetch_series)."""
    if frm:
        _dump_json(_window_path(table), {"from": frm, "table": table,
                                         "recorded_at_utc": datetime.now(timezone.utc).isoformat()})


def _group_by_ticker(rows):
    """Split a multi-ticker response into {ticker: [rows]}."""
    by = {}
    for r in rows:
        by.setdefault(r.get("ticker"), []).append(r)
    return by


def _read_existing(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _dump_json(out_path, data, indent=None):
    """Atomically write JSON: dump to a sibling .part, flush+fsync, then os.replace() into place."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def _write(out_path, data):
    _dump_json(out_path, data)


# --- ticker normalization: config carries EODHD-style tickers (BRK-B); Sharadar uses dots (BRK.B) ---

def _sharadar_ticker(t, cfg, table=None):
    """Config ticker -> Sharadar ticker.

    `ticker_overrides` is TABLE-SCOPED (ticker_override_tables, default SF1 only). Sharadar files
    FUNDAMENTALS under an issuer's primary share class — GOOG/FOX/NWS return 0 ARQ rows while
    GOOGL/FOXA/NWSA are populated — but SEP and ACTIONS carry each class separately and correctly.
    Applying the override to every table (as it first did) silently dropped GOOG/FOX/NWS from SEP
    and ACTIONS entirely: their files froze at the retired 1-year window and GOOG's genuine 20:1
    2022-07-18 split went missing from D-04. Verified live 2026-08-13: stocks/actions return rows
    for all three; only fundamentals is empty."""
    tables = cfg.get("ticker_override_tables") or ["SF1"]
    overrides = cfg.get("ticker_overrides") or {}
    if t in overrides and (table is None or table in tables):
        return overrides[t]
    rep = cfg.get("ticker_replace")
    if isinstance(rep, list) and len(rep) == 2:
        return t.replace(rep[0], rep[1])
    return t


def main():
    if not TOKEN:
        print("FATAL: SHARADAR_API_KEY not set", file=sys.stderr)
        return 1
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    tables = cfg.get("tables") or ["SEP", "SF1", "ACTIONS"]
    whole_tables = cfg.get("whole_tables") or []
    stocks = cfg.get("stocks", [])
    incremental = cfg.get("incremental", True)
    dimension = cfg.get("dimension", "ARQ")
    whole_refresh_days = cfg.get("whole_refresh_days", WHOLE_REFRESH_DAYS)

    unknown = [t for t in tables if t not in PER_TICKER]
    if unknown:
        print(f"FATAL: unknown per-ticker table(s) {unknown}; valid: {sorted(PER_TICKER)}", file=sys.stderr)
        return 1
    unknown_w = [t for t in whole_tables if t not in WHOLE_TABLES]
    if unknown_w:
        print(f"FATAL: unknown whole_table(s) {unknown_w}; valid: {sorted(WHOLE_TABLES)}", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    results = []
    retry_queue = []  # (index into results, attempt fn) per failed whole-table — end-of-run sweep

    def fetch_series(table):
        """Pull a per-ticker table for the WHOLE universe, then split into per-ticker files.
        COLD (some ticker has no file): multi-ticker calls over the date window, each capped at
        30 tickers / 200 chars by _ticker_batches. WARM (every ticker already on disk): one
        whole-universe lastupdated/date .gte pull (offset-paged), filtered to the universe.
        Everything is merged append-only (M1-01)."""
        spec = PER_TICKER[table]
        endpoint = ENDPOINT[table]
        incr_col = spec["incr_col"]
        syms = [_sharadar_ticker(t, cfg, table) for t in stocks]
        if not syms:
            return
        paths = {s: os.path.join(DATA_DIR, table, f"{s}.json") for s in syms}
        existing = {s: (_read_existing(paths[s]) if incremental else []) for s in syms}
        sf1_extra = {"dimension": dimension} if table == "SF1" else {}

        def persist(sym, new_rows, mode):
            entry = {"symbol": sym, "dataset": table, "ok": False, "count": 0, "added": 0, "error": None}
            try:
                merged, added = _merge(existing[sym], new_rows)
                _write(paths[sym], merged)
                entry.update(ok=True, count=len(merged), added=added)
                log(f"OK   {table:<14} {sym}: {len(merged)} (+{added}) [{mode}] -> {paths[sym]}")
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                log(f"FAIL {table:<14} {sym}: {entry['error']}")
            results.append(entry)

        # WARM only if every ticker has data AND a usable watermark (else fall back to cold batches).
        warm = incremental and all(existing[s] for s in syms)
        watermark = None
        frm = cfg.get(spec["from_key"]) or cfg.get("from")
        if warm:
            wms = [_latest(existing[s], incr_col) for s in syms]
            if any(w is None for w in wms):
                warm = False
            else:
                watermark = min(wms)
        if warm and frm:
            # WIDENING THE CONFIG WINDOW MUST ACTUALLY WIDEN IT. The warm path filters on
            # `<incr_col>.gte=<newest seen>`, which can only ever move FORWARD — so moving
            # `from`/`sf1_from` back in time would fetch nothing new and the operator's change
            # would silently do nothing. (Seen live: widening sf1_from 2025-07-28 -> 2021-07-28
            # left SF1 at 4 quarters/ticker.)
            #
            # Compare against the window we RECORDED, not against the oldest row we happen to
            # hold. Inferring it from row dates looks equivalent and is not: SF1's oldest
            # calendardate is 2021-09-30 (the first quarter-end at or after a 2021-07-28 start),
            # so "from < oldest row" is true forever and every run re-pulled the whole 5 years
            # instead of a top-up. Quarterly/event tables essentially never have a row exactly on
            # the window start; only daily price tables do.
            last = _read_window(table)
            if last is None:
                # Pre-marker volume: fall back to the row-date heuristic once, then the marker
                # written below makes every later run incremental.
                oldest = min((o for o in (_earliest(existing[s], spec["date_filter"]) for s in syms)
                              if o), default=None)
                stale = bool(oldest and frm < oldest)
                why = f"no window marker (oldest row {oldest})"
            else:
                stale = frm < last
                why = f"window widened {last} -> {frm}"
            if stale:
                log(f"     {table}: {why} — re-pulling the full window instead of an incremental top-up")
                warm = False
        try:
            if warm:
                base = dict(sf1_extra, **{f"{incr_col}.gte": watermark})
                rows, _ = _fetch(endpoint, base, label=f"bulk {incr_col}>={watermark}")
                uni = set(syms)
                by = _group_by_ticker([r for r in rows if r.get("ticker") in uni])
                for s in syms:
                    persist(s, by.get(s, []), f"incr(bulk) {incr_col}>={watermark}")
            else:
                for n, batch in enumerate(_ticker_batches(syms), start=1):
                    base = dict(sf1_extra, ticker=",".join(batch))
                    if frm:
                        base[f"{spec['date_filter']}.gte"] = frm
                    rows, _ = _fetch(endpoint, base, label=f"batch {n} ({len(batch)} tickers)")
                    by = _group_by_ticker(rows)
                    for s in batch:
                        persist(s, by.get(s, []), f"full {spec['date_filter']}>={frm}")
                _write_window(table, frm)   # this run covered [frm, now] for every ticker
        except Exception as e:
            # a batch/bulk request itself failed -> record it for every ticker not already written
            log(f"FAIL {table:<14}: {type(e).__name__}: {e}")
            done = {r["symbol"] for r in results if r["dataset"] == table}
            for s in syms:
                if s not in done:
                    results.append({"symbol": s, "dataset": table, "ok": False, "count": 0,
                                    "added": 0, "error": f"{type(e).__name__}: {e}"})

    def record_whole(table, out):
        """Whole-table snapshot (TICKERS / SP500): one high-limit call, replaced whole.

        On a warm volume, skip the re-pull when the on-disk file is younger than WHOLE_REFRESH_DAYS
        (the entity master / constituents change slowly). incremental:false forces a refetch.
        """
        endpoint = ENDPOINT[table]

        def attempt():
            entry = {"symbol": table, "dataset": table, "ok": False, "count": 0, "added": 0, "error": None}
            try:
                if incremental and whole_refresh_days > 0 and os.path.exists(out):
                    age_days = (time.time() - os.path.getmtime(out)) / 86400.0
                    if age_days < whole_refresh_days:
                        n = len(_read_existing(out))
                        entry.update(ok=True, count=n, added=0)
                        log(f"OK   {table:<14} ALL: {n} [fresh {age_days:.1f}d < {whole_refresh_days}d — skipped] -> {out}")
                        return entry
                rows, _ = _fetch(endpoint, dict(WHOLE_TABLES.get(table) or {}), label="ALL")
                _write(out, rows)
                entry.update(ok=True, count=len(rows), added=len(rows))
                log(f"OK   {table:<14} ALL: {len(rows)} [snapshot] -> {out}")
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                log(f"FAIL {table:<14} ALL: {entry['error']}")
            return entry
        entry = attempt()
        if not entry["ok"]:
            retry_queue.append((len(results), attempt))
        results.append(entry)

    def fetch_whole_market(table):
        """SURVIVORSHIP-FREE pull: the same table with NO ticker filter, just a date range.

        The per-ticker path above is bounded by the 503 CURRENT constituents, which is why ACTIONS
        held exactly one `delisted` row — a name that has already left the index can never appear.
        Spec §2 wants D-04 over "21k+ active+delisted tickers" and D-05 over "16k+ companies,
        survivorship-bias-free"; G-01's last-trade return and the T-12 delisted fixture both need it.

        Feasible because the retail API takes an unfiltered date-range query and pages by offset:
        ACTIONS is ~49k rows/year and SF1 ARQ ~22k/year, so five years is a few paged calls each.
        SEP is deliberately NOT offered here — whole-market daily prices would be ~63M rows, and
        eod_bulk already supplies survivorship-free prices for the same universe.
        """
        spec = PER_TICKER[table]
        frm = cfg.get(spec["from_key"]) or cfg.get("from")
        out = os.path.join(DATA_DIR, table, "_ALL.json")
        entry = {"symbol": f"{table}:_ALL", "dataset": f"{table}_whole", "ok": False,
                 "count": 0, "added": 0, "error": None}
        try:
            # An unfiltered JSON query is silently capped at a ~2-year default window regardless of
            # the .gte filter you pass (SF1 returned 21,781 rows / 2024-06-30 onward whatever the
            # date). Deep history comes from the vendor's BULK path: `years=N` 302-redirects to a
            # CSV zip. Measured: fundamentals-5Y.csv.zip is 118 MB, downloads in ~10 s, and yields
            # 122,047 ARQ rows back to 2020-03-31 across 7,669 tickers — versus 503 ticker-filtered.
            rows = _fetch_bulk_zip(ENDPOINT[table], WHOLE_MARKET_YEARS,
                                   dimension if table == "SF1" else None)
            out_gz = out + ".gz"
            n_tick = len({r.get("ticker") for r in rows})
            _dump_gz(out_gz, {"vendor": "Sharadar bulk", "table": table,
                              "years": WHOLE_MARKET_YEARS,
                              "dimension": dimension if table == "SF1" else None,
                              "pulled_at_utc": datetime.now(timezone.utc).isoformat(),
                              "n_rows": len(rows), "n_tickers": n_tick, "rows": rows})
            entry.update(ok=True, count=len(rows), added=len(rows))
            ds = sorted(str(r.get(spec["date_filter"]) or "")[:10] for r in rows if r.get(spec["date_filter"]))
            log(f"OK   {table:<14} _ALL: {len(rows):,} rows across {n_tick:,} tickers "
                f"[bulk years={WHOLE_MARKET_YEARS}] {ds[0] if ds else '-'}..{ds[-1] if ds else '-'} -> {out_gz}")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"FAIL {table:<14} _ALL: {entry['error']}")
        results.append(entry)

    # 1) per-ticker series (SEP / SF1 / ACTIONS) — batched over the universe, split to per-ticker files
    for table in tables:
        fetch_series(table)

    # 1b) the same tables WITHOUT the universe filter, so delistings and departed names exist at all
    for table in (cfg.get("whole_market_tables") or []):
        if table in PER_TICKER:
            fetch_whole_market(table)
        else:
            log(f"WARN whole_market_tables: {table} is not a per-ticker table — skipped")

    # 2) whole-table snapshots (TICKERS entity master, SP500 constituents) — survivorship-free
    for table in whole_tables:
        out = os.path.join(DATA_DIR, table, "SHARADAR.json")
        record_whole(table, out)

    # End-of-run retry sweep: vendor 5xx blips run minutes, so first-pass failures get one fresh
    # attempt before the run is judged. Per-ticker tables are fetched in whole-universe batches, so
    # their retry unit is the TABLE: drop its entries and re-run fetch_series (idempotent — atomic
    # writes, append-only merge over what's on disk). Whole-table retries go first, by index, while
    # results positions are still valid.
    failed_tables = sorted({r["dataset"] for r in results if not r["ok"] and r["dataset"] in PER_TICKER})
    if retry_queue or failed_tables:
        n_fail = len(retry_queue) + len(failed_tables)
        log(f"RETRY {n_fail} failed task(s)/table(s) after {RETRY_SWEEP_DELAY}s pause ...")
        time.sleep(RETRY_SWEEP_DELAY)
        healed = 0
        for idx, attempt in retry_queue:
            entry = attempt()
            entry["retried"] = True  # manifest: distinguishes healed-on-retry from clean first pass
            healed += 1 if entry["ok"] else 0
            results[idx] = entry
        for table in failed_tables:
            results[:] = [r for r in results if r["dataset"] != table]
            start = len(results)
            fetch_series(table)
            fresh = results[start:]
            for r in fresh:
                r["retried"] = True
            healed += 1 if fresh and all(r["ok"] for r in fresh) else 0
        log(f"RETRY sweep healed {healed}/{n_fail}")

    all_ok = bool(results) and all(r["ok"] for r in results)
    manifest = {
        "vendor": "Sharadar (api.sharadar.com retail API)",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "source_endpoint": f"{API}/{{endpoint}}",
        "from": cfg.get("from"),
        "sf1_from": cfg.get("sf1_from"),
        "dimension": dimension,
        "incremental": incremental,
        "whole_refresh_days": whole_refresh_days,
        "pace_sec": PACE_SEC,
        "batch_size": MAX_TICKERS_PER_CALL,
        "row_cap": ROW_CAP,
        "tables": tables,
        "whole_tables": whole_tables,
        "n_stocks": len(stocks),
        "ok": all_ok,
        "results": results,
    }
    _dump_json(os.path.join(DATA_DIR, "_run.json"), manifest, indent=2)

    if not all_ok:
        log(f"FAILED — error log: {_persist_log('error', manifest)}")
        return 1
    if STORE_LOGS:
        log(f"run log: {_persist_log('run', manifest)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            _LOG_LINES.append(traceback.format_exc())
            _persist_log("crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
