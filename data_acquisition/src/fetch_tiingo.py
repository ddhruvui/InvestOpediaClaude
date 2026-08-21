#!/usr/bin/env python3
"""Download every TIINGO dataset the v1.2 spec needs, for the universe in tiingo.json.

Pure stdlib (no pip). Covers the TIINGO column of
`../../Data Acquisition Specification — FINAL v1.2.md`. Per §1 + D-12 + G-04, Tiingo's role is
the **tertiary EOD-price cross-check vendor** (the D-12 tie-breaker behind EODHD-primary and
Sharadar-secondary), with News as the optional G-04 paid fix for pre-Dec-2020 headlines.
`scripts/launch.sh tiingo` runs it (EODHD is the sibling fetch.py, Sharadar fetch_nasdaq.py);
it writes to the `data_tiingo/` namespace on the same network volume.

PER-EQUITY (one file per ticker, driven by "stocks" + "datasets"; default datasets ["prices","metadata"]):
    prices    -> DATA_DIR/<TICKER>.json            D-01/D-12  daily OHLCV + adjusted OHLCV + divCash +
                                                   splitFactor. `close` is UNADJUSTED; the cross-check
                                                   factor is `adjClose/close` (mirrors EODHD's D-01). Also
                                                   carries divCash (D-03 cross-check) and splitFactor
                                                   (D-02 cross-check) in the same row.
    metadata  -> DATA_DIR/metadata/<TICKER>.json   entity/coverage cross-check: name, exchangeCode,
                                                   startDate, endDate, description (secondary to D-13).
    news      -> DATA_DIR/news/<TICKER>.json       G-04 (OPTIONAL, paid add-on) timestamped headlines;
                                                   APPEND-ONLY incremental like EODHD news. Off by default.

MARKET / INVENTORY LEVEL (separate config keys):
    market       -> DATA_DIR/market/<SYMBOL>.json          D-09 cross-check: SPY daily level (same price
                                                           fetcher; Tiingo uses the bare "SPY" symbol).
    symbol_list  -> DATA_DIR/symbols/supported_tickers.json D-13 cross-check: Tiingo's full supported-ticker
                                                           inventory incl. delisted (endDate in the past) —
                                                           drives cross-check-universe completeness. One
                                                           static CDN zip, parsed to JSON. No token needed.

Ticker format: Tiingo uses EODHD-style dashes for share classes (BRK-B), so the universe is shared
with config/tickers.json verbatim — no normalization step (unlike Sharadar's dash→dot).

INCREMENTAL (default on; set "incremental": false to force a full refetch). The network volume
persists DATA_DIR between RunPod launches, so the *append-only* stream — **news** — reads what's
already on disk and only adds newer rows (merged + deduped). Everything else refetches whole:
prices/metadata are tiny AND Tiingo rewrites the `adj*` columns retroactively after a split/dividend
so a naive append would go stale; the symbol list is a point-in-time snapshot replaced whole.

FREE-TIER PACING / RESUMABILITY (Tiingo free tier ≈ 50 req/hr, 1000/day, 500 unique symbols/mo):
    min_request_interval_sec  throttle: sleep to hold ≥ this gap between outgoing requests (default 0;
                              enforced PER TOKEN — see the two-account split below).
    max_requests_per_run      soft cap on outgoing requests per run (all tokens combined); remaining jobs
                              are marked DEFERRED (non-fatal) and a later run resumes them (0 = unlimited).
    skip_fresh_days           for the full-refetch datasets (prices/metadata/market/symbol_list), skip a
                              file already refreshed within N days — lets a capped/paced run chip away
                              across launches, newest gaps first (default 0 = always refetch, like EODHD).
News (append-only) ignores skip_fresh_days — it always fetches just its delta. A 429 (hourly window
exhausted) sleeps to the next window and retries, against a PER-TOKEN sleep budget
(TIINGO_MAX_RATE_SLEEPS, default 4/run): a healthy token that brushes the hourly cap loses at most
~1h; a token the vendor keeps 429ing is declared exhausted and its remaining jobs DEFER instantly
so the run still finishes and writes its manifest. (Before the budget, one broken token cost
6x900s per job and three consecutive nightly pods died at the 8h watchdog with no manifest.)

COVERAGE SIDECAR (`_coverage.json`): the widen-detection in _fresh() compares a file's first row
date against `from` — but a company that IPO'd after `from` can never have rows reaching it, so
those names looked permanently stale and were re-pulled full EVERY night (ABBV, ABNB, ISRG, …).
The sidecar records, per output file, the `from` a successful full fetch actually requested; a
file whose recorded window already contains the configured window is covered, no matter where its
rows start. Widening `from` still triggers exactly one refetch per name (which re-records).

TWO-ACCOUNT SPLIT (optional): set TIINGO_API_TOKEN2 and the universe is split half/half — the FIRST
half of "stocks" (list order) always uses token 1, the SECOND half always token 2, and the halves
are interleaved so each token paces its own ≈50 req/hr window (combined throughput doubles). The
split is POSITIONAL and must stay stable across runs within a month: the 500-unique-symbols cap is
sticky per account, so a ticker that switched accounts would burn a slot on both. market/symbol_list
jobs stay on token 1. With one token set, behavior is unchanged.

Self-termination is bootstrap.sh's job, so this runs/tests locally:

    DATA_DIR=./data_tiingo CONFIG_PATH=data_acquisition/config/tiingo.json \
      TIINGO_API_TOKEN=xxx STORE_LOGS=true python3 data_acquisition/src/fetch_tiingo.py

Logging (env-controlled): `_run.json` manifest is always written. A full run log
(`logs/run-<ts>.log`) is stored ONLY when `STORE_LOGS` is truthy. Failures (`logs/error-<ts>.log`)
and crashes (`logs/crash-<ts>.log`) are ALWAYS logged, regardless of `STORE_LOGS`.
Exit code: 0 if every job succeeded (deferred jobs are NOT failures), 1 otherwise.
"""
import csv
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
from datetime import date, datetime, timezone

# One token = whole universe; a second token (TIINGO_API_TOKEN2) splits it half/half (see docstring).
# A stored file may legitimately start later than the configured `from`: that date is a calendar
# day, the data starts on the first SESSION on or after it. Slack absorbs a New Year/holiday
# weekend without masking a real widening, which always moves the window by months or years.
STALE_GAP_TOLERANCE_DAYS = int(os.environ.get("TIINGO_STALE_GAP_DAYS", "14"))
TOKENS = [t for t in (os.environ.get("TIINGO_API_TOKEN", "").strip(),
                      os.environ.get("TIINGO_API_TOKEN2", "").strip()) if t]
DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_tiingo")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/tiingo.json")
API = "https://api.tiingo.com"
SUPPORTED_TICKERS_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
PRICE_FIELDS = ("date", "open", "high", "low", "close", "volume",
                "adjOpen", "adjHigh", "adjLow", "adjClose", "adjVolume", "divCash", "splitFactor")
PAGE_SIZE = 1000   # news page size
MAX_PAGES = 100    # hard backstop so offset pagination can never loop forever
# Vendor 5xx blips run minutes, not the seconds in-request retries cover — so first-pass failures
# get one more attempt at end of run, after this pause (seconds; env-overridable).
RETRY_SWEEP_DELAY = int(os.environ.get("RETRY_SWEEP_DELAY", "60"))
RATE_SLEEP = 900   # 429 = free-tier hourly window exhausted; sleep to the next window
# Per-token, per-run cap on those 900s sleeps. 4 = at most ~1h lost to a healthy token brushing
# the hourly cap; a token that is broken at the vendor (429 on every call, which sleeping cannot
# fix) is declared exhausted after 4 and its remaining jobs defer instantly.
MAX_RATE_SLEEPS = int(os.environ.get("TIINGO_MAX_RATE_SLEEPS", "4"))
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

_ctx = None  # default = verified TLS; falls back to unverified if the CA bundle is missing
_LOG_LINES = []
_req_count = 0    # api.tiingo.com requests this run, all tokens (the CDN symbol-list zip not counted)
_req_by_tok = {}  # per-token request counts (manifest visibility)
_last_req = {}    # per-token monotonic timestamp — each token paces its own rate window
_rate_sleeps = {}   # per-token count of 429 sleeps consumed this run
_dead_tokens = set()  # tokens whose sleep budget is spent — their jobs defer instead of sleeping


class BudgetExceeded(Exception):
    """Raised by _throttle when max_requests_per_run is spent — the job defers, never fails."""


class TokenExhausted(BudgetExceeded):
    """A token spent its 429-sleep budget — jobs on it defer (resume next run), never fail."""


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


def _throttle(tok, min_interval, max_requests):
    """Enforce the per-run request budget (all tokens) + per-TOKEN inter-request gap: with two
    accounts the halves interleave, so each token holds its own ≈50 req/hr window and the combined
    rate doubles."""
    global _req_count
    if tok in _dead_tokens:
        raise TokenExhausted(f"token {tok + 1} spent its 429-sleep budget this run")
    if max_requests and _req_count >= max_requests:
        raise BudgetExceeded(f"request budget spent ({_req_count}/{max_requests})")
    if min_interval > 0:
        gap = min_interval - (time.monotonic() - _last_req.get(tok, 0.0))
        if gap > 0:
            time.sleep(gap)
    _last_req[tok] = time.monotonic()
    _req_count += 1
    _req_by_tok[tok] = _req_by_tok.get(tok, 0) + 1


def _open(url, headers=None, tok=None):
    """GET url -> (status, raw_bytes). Retries transient errors; sleeps out 429 hourly windows
    against the per-token budget (tok=None = unauthenticated CDN asset, brief backoff only)."""
    global _ctx
    req = urllib.request.Request(url, headers=headers or {})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=120, context=_ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and tok is None:
                if attempt < 3:
                    attempt += 1
                    time.sleep(2 * attempt)
                    continue
                return e.code, b""
            if e.code == 429:
                try:
                    body = e.read().lower()
                except Exception:
                    body = b""
                # "monthly bandwidth allocation" 429s only clear at the month boundary — sleeping
                # is pure waste (measured live 2026-08-21: token 2 served exactly this body).
                if b"monthly" in body:
                    _dead_tokens.add(tok)
                    raise TokenExhausted(
                        f"token {tok + 1} is over its MONTHLY allocation (resets at month "
                        f"start) — deferring its remaining jobs")
                used = _rate_sleeps.get(tok, 0)
                if used >= MAX_RATE_SLEEPS:
                    _dead_tokens.add(tok)
                    raise TokenExhausted(
                        f"token {tok + 1} still 429 after {used} hourly-window sleeps — "
                        f"deferring its remaining jobs")
                _rate_sleeps[tok] = used + 1
                log(f"     429 rate-limited — sleeping {RATE_SLEEP}s for the hourly window "
                    f"(token {tok + 1}, sleep {used + 1}/{MAX_RATE_SLEEPS} this run)")
                time.sleep(RATE_SLEEP)
                continue
            if e.code in (500, 502, 503, 504) and attempt < 3:
                attempt += 1
                time.sleep(2 * attempt)
                continue
            return e.code, b""
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


def _auth_headers(tok):
    return {"Authorization": f"Token {TOKENS[tok]}", "Content-Type": "application/json",
            "User-Agent": "InvestOpediaClaude-DataAcquisition/1.0"}


def _api(path, params, throttle, tok=0):
    """Throttled GET {API}{path}?params with token #tok -> parsed JSON, raising on auth failure."""
    _throttle(tok, *throttle)
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    status, raw = _open(url, _auth_headers(tok), tok=tok)
    if status in (401, 403):
        raise RuntimeError(f"{status} — Tiingo token rejected (or endpoint not on this plan)")
    if status == 404:
        raise RuntimeError("404 — ticker not known to Tiingo")
    if status != 200:
        raise RuntimeError(f"unexpected response (status={status})")
    return json.loads(raw.decode("utf-8"))


# --- fetchers ----------------------------------------------------------------------------------

def _fetch_prices(symbol, from_date, throttle, tok=0):
    # D-01/D-12: unadjusted OHLCV + adjusted OHLCV + divCash + splitFactor in one row per session.
    params = {"format": "json", "resampleFreq": "daily"}
    if from_date:
        params["startDate"] = from_date
    payload = _api(f"/tiingo/daily/{urllib.parse.quote(symbol)}/prices", params, throttle, tok)
    if not isinstance(payload, list):
        raise RuntimeError("prices: expected list")
    return [{k: row.get(k) for k in PRICE_FIELDS} for row in payload]


def _fetch_news(symbol, from_date, throttle, tok=0):
    # G-04 (paid add-on): offset-paginated timestamped headlines for one ticker.
    rows = []
    offset = 0
    for page in range(1, MAX_PAGES + 1):
        params = {"tickers": symbol, "limit": PAGE_SIZE, "offset": offset}
        if from_date:
            params["startDate"] = from_date
        payload = _api("/tiingo/news", params, throttle, tok)
        if not isinstance(payload, list):
            raise RuntimeError("news: expected list")
        rows += payload
        if STORE_LOGS:
            log(f"     news {symbol} page {page}: +{len(payload)} ({len(rows)} total)")
        if len(payload) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE
    log(f"WARN news {symbol}: hit MAX_PAGES ({MAX_PAGES}) — older articles may be truncated")
    return rows


def fetch_metadata(symbol, throttle, tok=0):
    # Entity/coverage cross-check (secondary to D-13): name, exchangeCode, startDate, endDate.
    payload = _api(f"/tiingo/daily/{urllib.parse.quote(symbol)}", {"format": "json"}, throttle, tok)
    if not isinstance(payload, dict):
        raise RuntimeError("metadata: expected object")
    return payload


def fetch_supported_tickers():
    # D-13 cross-check: Tiingo's whole supported-ticker inventory (incl. delisted — endDate in the
    # past) as one static CDN zip of CSV. No token, no API request budget consumed.
    status, raw = _open(SUPPORTED_TICKERS_URL)
    if status != 200:
        raise RuntimeError(f"supported_tickers.zip: unexpected response (status={status})")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8"))
            return list(reader)


# Dedup key for the append-only news stream (a stable per-record identity).
def _news_key(r):
    return r.get("id") or r.get("url") or f"{r.get('publishedDate', '')}|{r.get('title', '')}"


def _news_date(r):
    return (r.get("publishedDate") or "")[:10]


def _read_existing(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _latest_date(rows):
    dates = [d for d in (_news_date(r) for r in rows) if d]
    return max(dates) if dates else None


def _merge(existing, new, keyfn):
    by = {keyfn(r): r for r in existing}
    added = 0
    for r in new:
        k = keyfn(r)
        if k not in by:
            added += 1
        by[k] = r
    rows = sorted(by.values(), key=lambda r: r.get("publishedDate") or r.get("date") or "")
    return rows, added


def _write(out_path, data, indent=None):
    """Atomically write JSON: dump to a sibling .part, flush+fsync, then os.replace() into place."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def _days_between(a, b):
    """b - a in days for two ISO dates; 0 if either is unparseable."""
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (TypeError, ValueError):
        return 0


_COVERAGE = None  # lazy {relpath: from_date a successful full fetch requested} — see docstring


def _coverage():
    global _COVERAGE
    if _COVERAGE is None:
        try:
            with open(os.path.join(DATA_DIR, "_coverage.json")) as f:
                d = json.load(f)
            _COVERAGE = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _COVERAGE = {}
    return _COVERAGE


def _record_coverage(path, want_from):
    """Remember that `path` was produced by a fetch that requested startDate=want_from."""
    if not want_from:
        return
    cov = _coverage()
    key = os.path.relpath(path, DATA_DIR)
    if cov.get(key) != want_from:
        cov[key] = want_from
        _write(os.path.join(DATA_DIR, "_coverage.json"), cov, indent=2)


def _fresh(path, skip_fresh_days, want_from=None):
    """True if the file exists and was refreshed within skip_fresh_days (resume across paced runs).

    `want_from` closes a trap: freshness is measured by MTIME, so a file written moments ago with
    a NARROWER window still looks fresh and gets skipped. Widening `from` in the config would then
    do nothing for skip_fresh_days (5 by default) — the operator changes the window, the next run
    reports "fresh — skipped" for every ticker, and the extra history never arrives.

    Whether the stored rows cover the window is decided by the `_coverage.json` sidecar (what a
    successful full fetch actually ASKED for), not by where the rows start: a post-2000 IPO name
    can never have rows reaching from=2000-01-01, and the old first-row heuristic declared every
    such name permanently stale — a full re-pull of ~150 tickers per night into a 50 req/hr free
    tier. The heuristic below survives only as the fallback for legacy files with no sidecar
    entry (each gets at most one more full fetch, which records its coverage)."""
    if skip_fresh_days <= 0 or not os.path.exists(path):
        return False
    if (time.time() - os.path.getmtime(path)) / 86400.0 >= skip_fresh_days:
        return False
    if want_from:
        got = _coverage().get(os.path.relpath(path, DATA_DIR))
        if got and got <= want_from:
            return True  # last full fetch already requested at least this window
        rows = _read_existing(path)
        dates = [str(r.get("date"))[:10] for r in rows if isinstance(r, dict) and r.get("date")]
        # TOLERANCE, not a bare `>`: `want_from` is a CALENDAR date, the data starts on the first
        # SESSION on or after it, so a fortnight of slack absorbs any New Year/holiday weekend.
        if dates and _days_between(want_from, min(dates)) > STALE_GAP_TOLERANCE_DAYS:
            return False
        _record_coverage(path, want_from)  # heuristic passed — record so we never re-read for it
    return True


def main():
    if not TOKENS:
        print("FATAL: TIINGO_API_TOKEN not set", file=sys.stderr)
        return 1
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    datasets = cfg.get("datasets") or ["prices", "metadata"]
    stocks = cfg.get("stocks", [])
    market = cfg.get("market", [])                 # bare symbols (SPY)
    symbol_list = cfg.get("symbol_list", False)    # bool: pull the supported-tickers inventory
    incremental = cfg.get("incremental", True)
    skip_fresh_days = cfg.get("skip_fresh_days", 0)
    throttle = (float(cfg.get("min_request_interval_sec", 0)),
                int(cfg.get("max_requests_per_run", 0)))

    valid = {"prices", "metadata", "news"}
    unknown = [d for d in datasets if d not in valid]
    if unknown:
        print(f"FATAL: unknown dataset(s) {unknown}; valid: {sorted(valid)}", file=sys.stderr)
        return 1

    os.makedirs(DATA_DIR, exist_ok=True)
    results = []
    retry_queue = []  # (index into results, attempt fn) per first-pass failure — end-of-run sweep

    def record(dataset, symbol, out, job, refetch_whole=True, want_from=None):
        """Run one job. skip_fresh_days short-circuits full-refetch jobs; BudgetExceeded defers."""
        def attempt():
            entry = {"symbol": symbol, "dataset": dataset, "ok": False, "count": 0, "added": 0,
                     "deferred": False, "error": None}
            try:
                if refetch_whole and _fresh(out, skip_fresh_days, want_from):
                    n = len(_read_existing(out))
                    entry.update(ok=True, count=n)
                    log(f"OK   {dataset:<10} {symbol}: {n} [fresh < {skip_fresh_days}d — skipped] -> {out}")
                    return entry
                data, added, mode = job()
                # A delisted/renamed name (or a vendor blip) can return an EMPTY payload with
                # HTTP 200 — never let that clobber real history (EQR: a full refetch overwrote
                # 6,695 rows with [] on 2026-08-21). Keep the existing file and say so.
                if isinstance(data, list) and not data and refetch_whole and _read_existing(out):
                    n = len(_read_existing(out))
                    entry.update(ok=True, count=n)
                    log(f"OK   {dataset:<10} {symbol}: vendor returned 0 rows — keeping the "
                        f"existing {n}-row file (delisted or vendor blip) -> {out}")
                    return entry
                _write(out, data)
                _record_coverage(out, want_from)
                c = len(data) if isinstance(data, (list, dict)) else 0
                entry.update(ok=True, count=c, added=added)
                log(f"OK   {dataset:<10} {symbol}: {c} (+{added}) [{mode}] -> {out}")
            except BudgetExceeded as e:
                entry.update(ok=True, deferred=True, error=str(e))
                log(f"DEFER {dataset:<9} {symbol}: {e} — a later run resumes")
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {e}"
                log(f"FAIL {dataset:<10} {symbol}: {entry['error']}")
            return entry
        entry = attempt()
        if not entry["ok"]:
            retry_queue.append((len(results), attempt))
        results.append(entry)

    def prices_job(symbol, from_date, tok=0):
        rows = _fetch_prices(symbol, from_date, throttle, tok)
        return rows, len(rows), f"full≥{from_date}" + (f" tok{tok + 1}" if len(TOKENS) > 1 else "")

    def news_job(symbol, out, tok=0):
        existing = _read_existing(out) if incremental else []
        since = (_latest_date(existing) or cfg.get("news_from", cfg.get("from"))) if existing \
            else cfg.get("news_from", cfg.get("from"))
        merged, added = _merge(existing, _fetch_news(symbol, since, throttle, tok), _news_key)
        return merged, added, (f"incr≥{since}" if existing else f"full≥{since}")

    # Two tokens => stable half/half split (POSITIONAL — the monthly unique-symbol cap is sticky per
    # account, so the mapping must not move within a month) + interleave so each token paces its own
    # rate window. One token => everything on token 1, original order.
    if len(TOKENS) == 2 and stocks:
        half = (len(stocks) + 1) // 2
        tok_of = {t: (0 if i < half else 1) for i, t in enumerate(stocks)}
        a, b = stocks[:half], stocks[half:]
        ordered = [t for pair in zip(a, b) for t in pair] + (a[len(b):] or b[len(a):])
        log(f"two-token split: {len(a)} tickers on token 1, {len(b)} on token 2 (interleaved)")
    else:
        tok_of = {t: 0 for t in stocks}
        ordered = stocks

    # 1) market-level cross-check first, on token 1 (SPY is the D-09 anchor — never let the
    #    per-ticker budget/symbol cap starve it)
    for sym in market:
        out = os.path.join(DATA_DIR, "market", f"{sym}.json")
        record("market", sym, out, lambda sym=sym: prices_job(sym, cfg.get("market_from", cfg.get("from"))),
               want_from=cfg.get("market_from", cfg.get("from")))

    # 2) per-equity datasets
    for ticker in ordered:
        tok = tok_of[ticker]
        for ds in datasets:
            if ds == "prices":
                out = os.path.join(DATA_DIR, f"{ticker}.json")
                record(ds, ticker, out, lambda t=ticker, k=tok: prices_job(t, cfg.get("from"), k),
                       want_from=cfg.get("from") if ds == "prices" else None)
            elif ds == "metadata":
                out = os.path.join(DATA_DIR, "metadata", f"{ticker}.json")
                record(ds, ticker, out,
                       lambda t=ticker, k=tok: (fetch_metadata(t, throttle, k), 1, "snapshot"))
            else:  # news — append-only incremental, ignores skip_fresh_days
                out = os.path.join(DATA_DIR, "news", f"{ticker}.json")
                record(ds, ticker, out, lambda t=ticker, o=out, k=tok: news_job(t, o, k),
                       refetch_whole=False)

    # 3) whole supported-ticker inventory (D-13 cross-check snapshot; CDN zip, budget-free)
    if symbol_list:
        out = os.path.join(DATA_DIR, "symbols", "supported_tickers.json")
        record("symbol_list", "ALL", out,
               lambda: (fetch_supported_tickers(), 0, "snapshot"))

    # End-of-run retry sweep: each first-pass failure gets one fresh attempt before the run is
    # judged. record() is idempotent (atomic writes, incremental re-reads disk), so a re-run is
    # safe; BudgetExceeded defers with ok=True and so is never swept.
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
    deferred = sum(1 for r in results if r.get("deferred"))
    manifest = {
        "vendor": "Tiingo",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "from": cfg.get("from"),
        "news_from": cfg.get("news_from"),
        "incremental": incremental,
        "datasets": datasets,
        "market": market,
        "symbol_list": bool(symbol_list),
        "min_request_interval_sec": throttle[0],
        "max_requests_per_run": throttle[1],
        "skip_fresh_days": skip_fresh_days,
        "tokens": len(TOKENS),
        "requests_used": _req_count,
        "requests_by_token": {f"token{k + 1}": v for k, v in sorted(_req_by_tok.items())},
        "rate_limit_sleeps": {f"token{k + 1}": v for k, v in sorted(_rate_sleeps.items())},
        "rate_limited_tokens": sorted(f"token{k + 1}" for k in _dead_tokens),
        "n_stocks": len(stocks),
        "deferred": deferred,
        "ok": all_ok,
        "results": results,
    }
    _write(os.path.join(DATA_DIR, "_run.json"), manifest, indent=2)

    if _dead_tokens:
        log(f"WARN token(s) {sorted(k + 1 for k in _dead_tokens)} spent their 429-sleep budget "
            f"({MAX_RATE_SLEEPS}) and had their remaining jobs deferred. If this repeats nightly, "
            f"the account is throttled at the vendor — check its usage/status on tiingo.com.")
    if deferred:
        log(f"NOTE: {deferred} job(s) deferred by the request budget — re-launch to continue")
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
