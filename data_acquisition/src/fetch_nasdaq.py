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
  - Paging  : NONE (no cursor/offset). A single call returns every matching row up to `limit`, so we
              pass a large LIMIT; if count == LIMIT we log a truncation WARN (never silently drop).
  - Limits  : ~500 requests / 900s, exposed via RateLimit-Remaining / RateLimit-Reset headers. We
              burst until nearly exhausted, then sleep until the window resets (see _pace).

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
from datetime import datetime, timezone

# SHARADAR_API_KEY is the sharadar.com retail key; NASDAQ_DATA_LINK_API_KEY kept as a back-compat alias.
TOKEN = os.environ.get("SHARADAR_API_KEY", "") or os.environ.get("NASDAQ_DATA_LINK_API_KEY", "")
DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_nasdaq")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/sharadar.json")
API = "https://api.sharadar.com/v1.0/data"
# logical table (spec/config) -> api.sharadar.com endpoint name
ENDPOINT = {"SEP": "stocks", "SF1": "fundamentals", "ACTIONS": "actions",
            "TICKERS": "tickers", "SP500": "sp500"}
LIMIT = int(os.environ.get("SHARADAR_LIMIT", "1000000"))  # single-call page size (API has no cursor)
# The API accepts a comma-list of tickers, so a cold backfill pulls the universe in BATCH_SIZE-ticker
# calls (500 tickers => ~5 calls/endpoint) instead of one-per-ticker; warm runs use a single
# whole-universe lastupdated.gte call per endpoint. Both keep us far under the 500-req/900s limit.
BATCH_SIZE = int(os.environ.get("SHARADAR_BATCH", "100"))
# ~500 req / 900s. Small polite gap between calls; _pace() additionally sleeps to the window reset
# when RateLimit-Remaining runs low, so a full-universe pass never trips the limit.
PACE_SEC = float(os.environ.get("SHARADAR_PACE_SEC", "0.05"))
RL_BUFFER = 3           # start waiting for the reset when this few requests remain in the window
WHOLE_REFRESH_DAYS = 7  # skip re-pulling a whole-table snapshot whose file is younger than this
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
WHOLE_TABLES = {"TICKERS", "SP500"}

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
    """GET url -> (status, parsed_json_or_None, headers_lower_dict). Retries transient errors + 429.

    On 429 we sleep for the RateLimit-Reset window (Sharadar's limit is per-900s, so a short backoff
    is pointless) and retry. TLS downgrades once if the CA bundle is missing (pod without certs).
    """
    global _ctx
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=120, context=_ctx) as r:
                headers = {k.lower(): v for k, v in r.headers.items()}
                return r.status, json.loads(r.read().decode("utf-8")), headers
        except urllib.error.HTTPError as e:
            headers = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            if e.code == 429 and attempt < 6:
                attempt += 1
                try:
                    wait = int(headers.get("ratelimit-reset")) + 2
                except (TypeError, ValueError):
                    wait = min(120, 30 * attempt)
                log(f"     429 rate-limited — sleeping {wait}s for the window reset (attempt {attempt})")
                time.sleep(wait)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 3:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            return e.code, None, headers
        except (ssl.SSLError, urllib.error.URLError, http.client.HTTPException,
                ConnectionError, TimeoutError) as e:
            err = e if isinstance(e, ssl.SSLError) else getattr(e, "reason", e)
            if _ctx is None and isinstance(err, ssl.SSLError):
                print("WARN: TLS verification failed, retrying without verification", flush=True)
                _ctx = ssl._create_unverified_context()
                continue
            if attempt < 4:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            raise


def _pace(headers):
    """Respect the ~500-req/900s limit: a small gap normally; sleep to the window reset when the
    RateLimit-Remaining header says we're nearly out. Falls back to the fixed pace if headers absent."""
    if PACE_SEC > 0:
        time.sleep(PACE_SEC)
    try:
        remaining = int(headers.get("ratelimit-remaining"))
    except (TypeError, ValueError):
        return
    if remaining <= RL_BUFFER:
        try:
            wait = int(headers.get("ratelimit-reset")) + 2
        except (TypeError, ValueError):
            wait = 60
        log(f"     rate-limit: {remaining} req left — sleeping {wait}s for the window reset")
        time.sleep(wait)


def _get(endpoint, params):
    """GET {API}/{endpoint}?{params}+api_key+format+limit -> parsed JSON, raising on failure."""
    q = dict(params, api_key=TOKEN, format="json", limit=LIMIT)
    url = f"{API}/{endpoint}?{urllib.parse.urlencode(q, safe=',')}"  # keep the ticker comma-list literal
    status, payload, headers = get_json(url)
    if status in (401, 403):
        raise RuntimeError(f"{status} — Sharadar key rejected for /{endpoint} "
                           f"(is SHARADAR_API_KEY a valid api.sharadar.com key with the bundle?)")
    if status == 404:
        raise RuntimeError(f"404 — no such endpoint /{endpoint}")
    if status != 200 or payload is None:
        raise RuntimeError(f"unexpected response (status={status})")
    _pace(headers)
    return payload


def _rows(payload):
    """Sharadar retail payload -> list of row dicts (already keyed; no columns/data zip)."""
    data = payload.get("data")
    return data if isinstance(data, list) else []


def _fetch(endpoint, params, label=None):
    """Single high-limit call (api.sharadar.com has no cursor). Returns (rows, truncated)."""
    payload = _get(endpoint, params)
    rows = _rows(payload)
    count = payload.get("count")
    truncated = isinstance(count, int) and count >= LIMIT
    if STORE_LOGS:
        log(f"     {endpoint} {label or ''}: {len(rows)} rows")
    return rows, truncated


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

def _sharadar_ticker(t, cfg):
    overrides = cfg.get("ticker_overrides") or {}
    if t in overrides:
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
        """Pull a per-ticker table for the WHOLE universe in a handful of calls, then split into
        per-ticker files. COLD (some ticker has no file): BATCH_SIZE-ticker multi-ticker calls over
        the date window. WARM (every ticker already on disk): ONE whole-universe lastupdated/date
        .gte call, filtered to the universe. Everything is merged append-only (M1-01)."""
        spec = PER_TICKER[table]
        endpoint = ENDPOINT[table]
        incr_col = spec["incr_col"]
        syms = [_sharadar_ticker(t, cfg) for t in stocks]
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
        if warm:
            wms = [_latest(existing[s], incr_col) for s in syms]
            if any(w is None for w in wms):
                warm = False
            else:
                watermark = min(wms)
        try:
            if warm:
                base = dict(sf1_extra, **{f"{incr_col}.gte": watermark})
                rows, truncated = _fetch(endpoint, base, label=f"bulk {incr_col}>={watermark}")
                if truncated:
                    log(f"WARN {table}: count hit LIMIT ({LIMIT}) — rows may be truncated")
                uni = set(syms)
                by = _group_by_ticker([r for r in rows if r.get("ticker") in uni])
                for s in syms:
                    persist(s, by.get(s, []), f"incr(bulk) {incr_col}>={watermark}")
            else:
                frm = cfg.get(spec["from_key"]) or cfg.get("from")
                for i in range(0, len(syms), BATCH_SIZE):
                    batch = syms[i:i + BATCH_SIZE]
                    base = dict(sf1_extra, ticker=",".join(batch))
                    if frm:
                        base[f"{spec['date_filter']}.gte"] = frm
                    n = i // BATCH_SIZE + 1
                    rows, truncated = _fetch(endpoint, base, label=f"batch {n} ({len(batch)} tickers)")
                    if truncated:
                        log(f"WARN {table} batch {n}: count hit LIMIT ({LIMIT}) — rows may be truncated")
                    by = _group_by_ticker(rows)
                    for s in batch:
                        persist(s, by.get(s, []), f"full {spec['date_filter']}>={frm}")
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
                rows, truncated = _fetch(endpoint, {}, label="ALL")
                if truncated:
                    log(f"WARN {table}: count hit LIMIT ({LIMIT}) — rows may be truncated")
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

    # 1) per-ticker series (SEP / SF1 / ACTIONS) — batched over the universe, split to per-ticker files
    for table in tables:
        fetch_series(table)

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
        "batch_size": BATCH_SIZE,
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
