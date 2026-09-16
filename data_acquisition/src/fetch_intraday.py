#!/usr/bin/env python3
"""EODHD 1-minute intraday bars (extended hours included) for every stock we hold -> Parquet.

Spec: Two Auctions a Day memo, section 7 ("The 1-minute pull plan"). Pull ONE-minute bars, the only
EODHD interval with history before October 2020 and the only one documented to include pre-market
(04:00 ET) and after-hours (to 20:00 ET). Everything coarser is resampled downstream, never pulled.

UNIVERSE: the union of config "universe_configs" (tickers.json + watchlist_eodhd.json: the S&P list
and the data-only watchlist), this config's own "stocks" (pulled first) and "market" (SPY, QQQ).
Reading the other configs means an index change or a new watchlist name flows into the minute store
on the next run with no edit here. A name that leaves the lists keeps its files; it just stops
being topped up.

LAYOUT (root = DATA_DIR, normally /workspace/data/tickdata — see FOLDER RULE):
    <root>/1m/<SYMBOL>/<YYYY>.parquet   one file per symbol-year (ET year): ts (int64, Unix UTC s),
                                          gmtoffset (int32, s: -14400 EDT / -18000 EST, COMPUTED here
                                          from America/New_York — the vendor sends UTC stamps with
                                          gmtoffset 0 — so ET wall clock is ts+gmtoffset everywhere),
                                          open/high/low/close (float64), volume (float64)
    <root>/_manifest.json                 producer marker + per-symbol {first_ts, last_ts, rows,
                                          complete, requests_total, no_vendor_data_until}; RESUME point
    <root>/_audit/<SYMBOL>.json           per-file audit cache (size/mtime key, sessions present,
                                          structural counts) + the gap sessions already tried once
    <root>/_run.json                      per-run results
    <root>/_verify.json                   full-history verification: session coverage vs the XNYS
                                          calendar, structure, extended hours, 09:30 bar vs daily open
    <root>/logs/                          run/error/crash logs (run logs only when STORE_LOGS)

APPEND-ONLY (user requirement): bars already on disk are never replaced or removed. A pull merges by
timestamp with the EXISTING bar winning; only timestamps the store does not have are added. A year
file that gains nothing is not touched at all (same bytes, same mtime); a year file that gains bars
is rebuilt atomically (tmp + rename) from its existing rows, unchanged, plus the new ones — Parquet
has no in-place append.

WHAT GETS ADDED each run, per symbol:
  1. tail   — from manifest last_ts minus RESETTLE_DAYS to now (the vendor fills a session for hours
              after the close, so the trailing sessions are re-asked and their missing minutes added).
              A symbol with no data backfills from config "from" (2004-01-02) or its daily history's
              first date, whichever is later. Cold symbols first probe the latest 10 days: an empty
              answer marks the symbol "no vendor data" (rechecked every NO_DATA_RETRY_DAYS) instead of
              walking 70 empty windows.
  2. gaps   — XNYS sessions inside a symbol's span with no bars at all. Each missing session is asked
              for ONCE (grouped into 120-day windows); what the vendor still lacks is recorded as
              unfillable in _audit and never re-billed.

BUDGET: EODHD bills 5 credits per intraday request. The /user preflight caps the run at what the day
allows after RESERVE_CREDITS (default 25k, so the nightly eodhd pod — ~14k — is never starved when
both run at once), re-checked every 500 requests. A capped run exits 0 with budget_exhausted.
TIME: MAX_RUN_MINUTES (default 430) stops cleanly inside bootstrap.sh's 8h watchdog; time_capped.

SPACE (user requirement): free space is checked before every request and every write. Below
MIN_FREE_GB the fetcher grows the network volume by GROW_STEP_GB (1) through the RunPod API, waits
for the new size, re-checks, and repeats — at most MAX_GROW_GB_PER_RUN. Free space comes from statvfs
when the mount reports the volume's own size, else allocated size (API) minus a walk of /workspace.
If growing is impossible (no key, API refusal, cap reached) or a write hits ENOSPC/EDQUOT, the run
stops with exit 75 (NEED_SPACE); the host runner grows and relaunches.

Deps: pyarrow (+ tzdata), installed by bootstrap.sh via PIP_PACKAGES. Everything else stdlib.
Exit codes: 0 ok (possibly budget/time capped), 1 symbol failures or verification failed, 75 space.
Local test:
    DATA_DIR=./out/tickdata CONFIG_PATH=config/intraday.json EODHD_API_TOKEN=xxx python src/fetch_intraday.py
"""
import errno
import io
import json
import os
import ssl
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

TOKEN = os.environ.get("EODHD_API_TOKEN", "")
DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data/tickdata")
ALT_DATA_DIR = os.environ.get("ALT_DATA_DIR", os.path.join(os.path.dirname(DATA_DIR.rstrip("/")), "intraday_1m"))
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/intraday.json")
DAILY_DIR = os.environ.get("DAILY_DIR", "/workspace/data")          # per-ticker daily bars: listing hint + open check
SESSIONS_PATH = os.environ.get("SESSIONS_PATH", "/workspace/data_calendar/XNYS.json")
VOLUME_ROOT = os.environ.get("VOLUME_ROOT", "/workspace")
VOLUME_ID = os.environ.get("NETWORK_VOLUME_ID", "")
RUNPOD_KEY = os.environ.get("RUNPOD_TERMINATE_KEY", "") or os.environ.get("RUNPOD_API_KEY", "")
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")
API = "https://eodhd.com/api"
UA = "investopediaclaude-intraday/2.0"      # never Python-urllib/*: Cloudflare 1010s it on rest.runpod.io
CREDITS_PER_REQUEST = 5
EXIT_NEED_SPACE = 75
PRODUCER = "fetch_intraday"
DAY = 86400
REG_OPEN, REG_CLOSE = 9 * 60 + 30, 16 * 60          # ET minutes
EXT_OPEN, EXT_CLOSE = 4 * 60, 20 * 60                # bars stamped 04:00 .. 20:00 inclusive

_ctx = None      # default verified TLS; certifi's bundle if present; unverified fallback once, if no CA bundle
try:
    import certifi  # noqa: F401
    _ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    pass
_LOG_LINES = []
_LOG_LOCK = threading.Lock()


def log(msg):
    with _LOG_LOCK:
        print(msg, flush=True)
        _LOG_LINES.append(msg)


def _persist_log(root, kind, manifest=None):
    d = os.path.join(root, "logs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log")
    with open(p, "w") as f:
        f.write("\n".join(_LOG_LINES))
        if manifest is not None:
            f.write("\n\n--- manifest ---\n" + json.dumps(manifest, indent=2, default=str))
    return p


class CreditsExhausted(Exception):
    pass


class TickerNotFound(Exception):
    pass


class NeedSpace(Exception):
    pass


def _now():
    """The data horizon ("now" for windows and the last completed session). Tests pin it."""
    return time.time()


def _cfg(cfg, key, env, default, cast):
    v = os.environ.get(env, "").strip()
    return cast(v) if v else cast(cfg.get(key, default))


# ---------------------------------------------------------------- http
def http_get(url, headers=None, retries=4, method="GET", body=None, timeout=120):
    """-> (status, bytes). Retries 429/5xx and socket errors; returns 4xx other than 402 to the caller.
    Verified TLS with a one-time unverified fallback when the image lacks a CA bundle (as fetch.py)."""
    global _ctx
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=body, method=method, headers={"User-Agent": UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                last = f"HTTP {e.code}"; time.sleep(3 * (i + 1)); continue
            return e.code, e.read()
        except (ssl.SSLError, urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            # urllib wraps the SSL failure in URLError, so match on the message, not the type
            if "CERTIFICATE_VERIFY_FAILED" in str(e) and not getattr(_ctx, "_unverified", False):
                log("     TLS: no usable CA bundle — falling back to an unverified context (verification-only downgrade)")
                _ctx = ssl._create_unverified_context(); _ctx._unverified = True; continue
            last = str(e)[:120]; time.sleep(3 * (i + 1))
    raise RuntimeError(f"gave up after {retries} attempts: {last}")


def credit_status():
    """(used, cap) from /user, honouring extraLimit and the lazy counter reset (see fetch.py)."""
    try:
        st, b = http_get(f"{API}/user?api_token={TOKEN}&fmt=json")
        d = json.loads(b.decode())
        used, cap = int(d["apiRequests"]), int(d["dailyRateLimit"]) + int(d.get("extraLimit") or 0)
        if str(d.get("apiRequestsDate", ""))[:10] != datetime.now(timezone.utc).date().isoformat():
            return 0, cap
        return used, cap
    except Exception as e:
        log(f"     credit status unavailable ({str(e)[:80]})")
        return None, None


class Budget:
    """Request allowance shared by the workers; re-reads the live counter every REFRESH requests so a
    concurrent eodhd pod's spend is seen and RESERVE_CREDITS stays untouched."""
    REFRESH = 500

    def __init__(self, left, reserve):
        self.left, self.reserve, self.used, self._lock = left, reserve, 0, threading.Lock()

    def take(self):
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= 1; self.used += 1
            refresh = self.used % self.REFRESH == 0
        if refresh:
            used, cap = credit_status()
            if used is not None and cap:
                live = max(0, (cap - self.reserve - used) // CREDITS_PER_REQUEST)
                with self._lock:
                    if live < self.left:
                        log(f"     credits: live counter {used:,}/{cap:,} -> request budget {self.left:,} -> {live:,}")
                        self.left = live
        return True

    def stop(self):
        with self._lock:
            self.left = 0

    @property
    def exhausted(self):
        return self.left <= 0


# ---------------------------------------------------------------- space
class Space:
    """Free space on the network volume, and growing it GROW_STEP_GB at a time when it runs low."""

    def __init__(self, root, min_free_gb, step_gb, max_grow_gb):
        self.root, self.min_free, self.step, self.max_grow = root, min_free_gb, step_gb, max_grow_gb
        self.grown = 0
        self._lock = threading.Lock()
        self._walk_used = None; self._walk_at = 0.0; self._written = 0
        self.alloc = self._api_size()
        tot, free = self._statvfs()
        if self.alloc is not None and abs(tot - self.alloc) <= max(2.0, 0.05 * self.alloc):
            self.mode = "statvfs"
        elif self.alloc is not None:
            self.mode = "accounted"
        else:
            self.mode = "statvfs-only"          # no API: ENOSPC/EDQUOT is the backstop
        log(f"     space: statvfs total {tot:,.1f} GB free {free:,.1f} GB; volume {VOLUME_ID or '?'} allocated "
            f"{self.alloc if self.alloc is not None else '?'} GB -> mode {self.mode}; free now {self.free_gb():.2f} GB, "
            f"floor {self.min_free} GB, grow step {self.step} GB (max {self.max_grow} GB this run)")

    def _statvfs(self):
        try:
            s = os.statvfs(self.root)
            return s.f_blocks * s.f_frsize / 2 ** 30, s.f_bavail * s.f_frsize / 2 ** 30
        except Exception:
            return float("inf"), float("inf")

    def _api(self, method="GET", size=None):
        if not (VOLUME_ID and RUNPOD_KEY):
            return None
        body = json.dumps({"size": size}).encode() if size is not None else None
        try:
            st, b = http_get(f"https://rest.runpod.io/v1/networkvolumes/{VOLUME_ID}", method=method, body=body, retries=3,
                             headers={"Authorization": f"Bearer {RUNPOD_KEY}", "Content-Type": "application/json"}, timeout=30)
            if st in (200, 201):
                return json.loads(b.decode())
            log(f"     volume API {method} -> HTTP {st}: {b[:160]!r}")
        except Exception as e:
            log(f"     volume API {method} failed: {str(e)[:120]}")
        return None

    def _api_size(self):
        d = self._api()
        return int(d["size"]) if d and "size" in d else None

    def _walk(self):
        total = 0
        stack = [VOLUME_ROOT]
        while stack:
            p = stack.pop()
            try:
                with os.scandir(p) as it:
                    for e in it:
                        try:
                            if e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                total += e.stat(follow_symlinks=False).st_size
                        except OSError:
                            continue
            except OSError:
                continue
        return total / 2 ** 30

    def note_written(self, nbytes):
        with self._lock:
            self._written += nbytes

    def free_gb(self):
        if self.mode != "accounted":
            return self._statvfs()[1]
        est = None
        if self._walk_used is not None:
            est = self.alloc - self._walk_used - self._written / 2 ** 30
        # re-walk when the estimate nears the floor (other pods write too) or it is 15 min old
        if est is None or time.time() - self._walk_at > 900 or (est < 2 * self.min_free + self.step and time.time() - self._walk_at > 120):
            t0 = time.time(); self._walk_used = self._walk(); self._walk_at = time.time(); self._written = 0
            est = self.alloc - self._walk_used
            if time.time() - t0 > 30:
                log(f"     space: walked {VOLUME_ROOT} in {time.time() - t0:.0f}s -> {self._walk_used:.2f} GB used")
        return est

    def ensure(self):
        """True when at least MIN_FREE_GB is free, growing the volume as needed; False = cannot."""
        with self._lock:
            free = self.free_gb()
            while free < self.min_free:
                if self.mode == "statvfs-only":
                    log(f"NEED_SPACE: {free:.2f} GB free and no volume API (NETWORK_VOLUME_ID/key) to grow it"); return False
                if self.grown + self.step > self.max_grow:
                    log(f"NEED_SPACE: {free:.2f} GB free and this run already grew {self.grown} GB (cap {self.max_grow})"); return False
                if not self._grow_once():
                    return False
                free = self.free_gb()
                log(f"     space: {free:.2f} GB free after growing (+{self.grown} GB this run)")
            return True

    def _grow_once(self):
        cur = self._api_size()
        if cur is None:
            log("NEED_SPACE: volume size unreadable — cannot grow"); return False
        new = cur + self.step
        tot_before = self._statvfs()[0]
        log(f"     space: below {self.min_free} GB free — growing volume {VOLUME_ID} {cur} GB -> {new} GB")
        if self._api("PATCH", new) is None:
            log("NEED_SPACE: grow request refused"); return False
        for _ in range(18):                      # API size first ...
            time.sleep(5)
            now = self._api_size()
            if now is not None and now >= new:
                break
        else:
            log(f"NEED_SPACE: volume size did not reach {new} GB within 90s"); return False
        self.alloc = now; self.grown += self.step
        if self.mode == "statvfs":               # ... then the mount must see it too
            for _ in range(36):
                if self._statvfs()[0] >= tot_before + 0.9 * self.step:
                    break
                time.sleep(5)
            else:
                log("     space: mount size did not follow the API within 180s — switching to allocated-minus-used accounting")
                self.mode = "accounted"; self._walk_used = None
        return True


# ---------------------------------------------------------------- Eastern time
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")       # needs system zoneinfo or the tzdata wheel (PIP_PACKAGES)
except Exception:
    _ET = None
_OFF_CACHE = {}


def _dst_rule_offset(ts):
    """Fallback US Eastern offset without tzdata: DST from the second Sunday of March 02:00 to the
    first Sunday of November 02:00 (2007+); before 2007 first Sunday of April to last Sunday of October."""
    d = datetime.fromtimestamp(ts, tz=timezone.utc); y = d.year

    def nth_sunday(month, n):
        first = date(y, month, 1); off = (6 - first.weekday()) % 7
        return date(y, month, 1 + off + 7 * (n - 1))

    def last_sunday(month):
        nxt = date(y + (month == 12), (month % 12) + 1, 1); dd = nxt - timedelta(days=1)
        return dd - timedelta(days=(dd.weekday() + 1) % 7)
    if y >= 2007:
        start, end = nth_sunday(3, 2), nth_sunday(11, 1)
    else:
        start, end = nth_sunday(4, 1), last_sunday(10)
    start_ts = datetime(start.year, start.month, start.day, 7, tzinfo=timezone.utc).timestamp()   # 02:00 EST = 07:00 UTC
    end_ts = datetime(end.year, end.month, end.day, 6, tzinfo=timezone.utc).timestamp()           # 02:00 EDT = 06:00 UTC
    return -14400 if start_ts <= ts < end_ts else -18000


def et_offset_for_utc_day(day):
    """Eastern offset for every bar stamped on UTC day `day` (days since epoch). US clocks change on a
    Sunday at 06:00/07:00 UTC, when nothing trades, so one value per UTC day is exact for market bars —
    the same values the v1 fetcher cached per day."""
    off = _OFF_CACHE.get(day)
    if off is None:
        ts = day * DAY + 12 * 3600
        off = int(datetime.fromtimestamp(ts, tz=_ET).utcoffset().total_seconds()) if _ET is not None else _dst_rule_offset(ts)
        _OFF_CACHE[day] = off
    return off


def et_offset(ts):
    return et_offset_for_utc_day(int(ts) // DAY)


# ---------------------------------------------------------------- parquet
def _pa():
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    return pa, pc, pq


SCHEMA_FIELDS = (("ts", "int64"), ("gmtoffset", "int32"), ("open", "float64"), ("high", "float64"),
                 ("low", "float64"), ("close", "float64"), ("volume", "float64"))


def schema():
    pa, _, _ = _pa()
    return pa.schema([(n, getattr(pa, t)()) for n, t in SCHEMA_FIELDS])


def empty_table():
    pa, _, _ = _pa()
    return schema().empty_table()


def parse_csv(body):
    """Vendor CSV (Timestamp,Gmtoffset,Datetime,Open,High,Low,Close,Volume) -> table in SCHEMA order.
    Same rules as the v1 JSON parser: rows without open or close are dropped, a missing high/low takes
    the open, a missing volume is 0. gmtoffset is recomputed (the vendor sends 0)."""
    pa, pc, _ = _pa()
    import pyarrow.csv as pcsv
    text = body.lstrip()
    if not text:
        return empty_table()
    if not text.startswith(b"Timestamp"):
        raise RuntimeError(f"unexpected vendor body: {text[:80]!r}")
    if b"\n" not in text.strip():             # header only: no bars in the window
        return empty_table()
    f64 = pa.float64()
    t = pcsv.read_csv(io.BytesIO(text), convert_options=pcsv.ConvertOptions(
        include_columns=["Timestamp", "Open", "High", "Low", "Close", "Volume"],
        column_types={"Timestamp": pa.int64(), "Open": f64, "High": f64, "Low": f64, "Close": f64, "Volume": f64}))
    if t.num_rows == 0:
        return empty_table()
    t = t.filter(pc.and_(pc.is_valid(t["Open"]), pc.is_valid(t["Close"])))
    t = t.filter(pc.is_valid(t["Timestamp"]))
    if t.num_rows == 0:
        return empty_table()
    ts = t["Timestamp"].combine_chunks()
    days = pc.divide(ts, DAY)
    uniq = pc.unique(days)
    offs = pa.array([et_offset_for_utc_day(int(d)) for d in uniq.to_pylist()], pa.int32())
    gmt = pc.take(offs, pc.index_in(days, value_set=uniq))
    op = t["Open"].combine_chunks()
    return pa.table([ts, gmt, op, pc.fill_null(t["High"].combine_chunks(), op), pc.fill_null(t["Low"].combine_chunks(), op),
                     t["Close"].combine_chunks(), pc.fill_null(t["Volume"].combine_chunks(), 0.0)], schema=schema())


def et_years(tbl):
    pa, pc, _ = _pa()
    return pc.year(pc.cast(pc.add(tbl["ts"], pc.cast(tbl["gmtoffset"], pa.int64())), pa.timestamp("s")))


def split_by_year(tbl):
    """-> {year: table}"""
    _, pc, _ = _pa()
    if tbl.num_rows == 0:
        return {}
    yrs = et_years(tbl)
    return {int(y): tbl.filter(pc.equal(yrs, y)) for y in pc.unique(yrs).to_pylist()}


def dedupe_sorted(tbl):
    """Sort by ts and keep the FIRST row of each timestamp (earlier chunks win)."""
    pa, pc, _ = _pa()
    if tbl.num_rows <= 1:
        return tbl
    tbl = tbl.combine_chunks()
    tbl = tbl.take(pc.sort_indices(tbl, sort_keys=[("ts", "ascending")]))   # stable: first chunk first
    ts = tbl["ts"].combine_chunks()
    keep = pc.not_equal(ts.slice(1), ts.slice(0, len(ts) - 1))
    return tbl.filter(pa.concat_arrays([pa.array([True]), keep]))


def read_year(path):
    _, _, pq = _pa()
    return pq.read_table(path).cast(schema()) if os.path.exists(path) else None


def merge_year(path, new, space=None):
    """APPEND-ONLY merge of `new` into one year file. Existing bars always win; the file is not
    touched when nothing is new. Returns the number of bars added."""
    pa, pc, pq = _pa()
    new = dedupe_sorted(new)
    if new.num_rows == 0:
        return 0
    existing = read_year(path)
    if existing is not None and existing.num_rows:
        new = new.filter(pc.invert(pc.is_in(new["ts"], value_set=existing["ts"].combine_chunks())))
        if new.num_rows == 0:
            return 0
        combined = pa.concat_tables([existing, new]).combine_chunks()
        combined = combined.take(pc.sort_indices(combined, sort_keys=[("ts", "ascending")]))
        n_before = existing.num_rows
    else:
        combined, n_before = new, 0
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    old_size = os.path.getsize(path) if os.path.exists(path) else 0
    try:
        pq.write_table(combined, tmp, compression="zstd")
        if pq.read_metadata(tmp).num_rows != n_before + new.num_rows:
            raise RuntimeError(f"{path}: rewritten file row count mismatch")
        new_size = os.path.getsize(tmp)
        os.replace(tmp, path)
    except OSError as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        if e.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", 122)):
            raise NeedSpace(str(e))
        raise
    if space is not None:
        space.note_written(new_size - old_size)
    return new.num_rows


def year_rows(path):
    _, _, pq = _pa()
    return pq.read_metadata(path).num_rows if os.path.exists(path) else 0


def parquet_years(sym_dir):
    if not os.path.isdir(sym_dir):
        return []
    return sorted(int(f[:4]) for f in os.listdir(sym_dir) if f.endswith(".parquet") and f[:4].isdigit())


# ---------------------------------------------------------------- vendor
def vendor_symbol(cfg, symbol):
    return (cfg.get("vendor_symbols") or {}).get(symbol, symbol)


def fetch_window(cfg, symbol, ts_from, ts_to):
    url = (f"{API}/intraday/{urllib.parse.quote(vendor_symbol(cfg, symbol))}.US?interval=1m&from={int(ts_from)}"
           f"&to={int(ts_to)}&fmt=csv&api_token={TOKEN}")
    st, body = http_get(url)
    if st == 402:
        raise CreditsExhausted("HTTP 402: EODHD daily credits exhausted")
    if st == 404:
        raise TickerNotFound(f"HTTP 404: {body[:40].decode(errors='replace').strip()}")
    if st != 200:
        raise RuntimeError(f"HTTP {st}: {body[:80]!r}")
    return parse_csv(body)


# ---------------------------------------------------------------- calendar
def load_sessions():
    try:
        return sorted(json.load(open(SESSIONS_PATH)).get("sessions", []))
    except Exception as e:
        log(f"     XNYS sessions unavailable ({str(e)[:80]}) — gap checks skipped")
        return []


def last_completed_session(sessions, now_ts=None):
    """Latest XNYS session whose after-hours (20:00 ET) is over."""
    now_ts = now_ts or _now()
    et_now = datetime.fromtimestamp(now_ts + et_offset(now_ts), tz=timezone.utc)
    today = et_now.date().isoformat()
    cut = today if et_now.hour >= 20 else (et_now.date() - timedelta(days=1)).isoformat()
    past = [s for s in sessions if s <= cut]
    return past[-1] if past else None


def ord_day(iso):
    return (date.fromisoformat(iso) - date(1970, 1, 1)).days


def iso_day(n):
    return (date(1970, 1, 1) + timedelta(days=int(n))).isoformat()


# ---------------------------------------------------------------- store (manifest + audits)
class Store:
    def __init__(self, root):
        self.root = root
        self._lock = threading.Lock(); self._saved_at = 0.0
        p = os.path.join(root, "_manifest.json")
        m = None
        try:
            m = json.load(open(p))
            if m.get("producer") != PRODUCER:
                m = None
        except Exception:
            pass
        self.m = m or {"producer": PRODUCER, "interval": "1m", "symbols": {}, "created_at": datetime.now(timezone.utc).isoformat()}

    def entry(self, s):
        with self._lock:
            return self.m["symbols"].setdefault(s, {})

    def update(self, s, **kw):
        with self._lock:
            self.m["symbols"].setdefault(s, {}).update(kw)

    def save(self, force=False):
        with self._lock:
            if not force and time.time() - self._saved_at < 20:
                return
            self.m["updated_at"] = datetime.now(timezone.utc).isoformat()
            p = os.path.join(self.root, "_manifest.json"); tmp = p + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.m, f, indent=1)
            os.replace(tmp, p)
            self._saved_at = time.time()

    def audit_path(self, s):
        return os.path.join(self.root, "_audit", f"{s}.json")

    def load_audit(self, s):
        try:
            return json.load(open(self.audit_path(s)))
        except Exception:
            return {"files": {}, "tried_days": []}

    def save_audit(self, s, a):
        p = self.audit_path(s); os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(a, f, separators=(",", ":"))
        os.replace(tmp, p)


# ---------------------------------------------------------------- audit / verification
def audit_file(path, year, n_open_days):
    """Full read of one year file: sessions present + structural counts + last-session profile."""
    pa, pc, pq = _pa()
    t = pq.read_table(path).cast(schema())
    n = t.num_rows
    st = os.stat(path)
    out = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "rows": n}
    if n == 0:
        return {**out, "days": [], "unsorted": 0, "dup_ts": 0, "bad_offset": 0, "wrong_year": 0, "out_of_hours": 0, "bad_ohlc": 0}
    ts = t["ts"].combine_chunks(); off64 = pc.cast(t["gmtoffset"], pa.int64())
    local = pc.add(ts, off64)
    diffs = pc.subtract(ts.slice(1), ts.slice(0, n - 1)) if n > 1 else pa.array([], pa.int64())
    days = pc.divide(local, DAY)
    mins = pc.divide(pc.subtract(local, pc.multiply(days, DAY)), 60)
    o, h, lo, c = (t[k].combine_chunks() for k in ("open", "high", "low", "close"))
    bad_ohlc = pc.or_(pc.or_(pc.less_equal(lo, 0), pc.less(h, lo)),
                      pc.or_(pc.greater(pc.max_element_wise(o, c), pc.multiply(h, 1.000001)),
                             pc.less(pc.min_element_wise(o, c), pc.multiply(lo, 0.999999))))
    out.update(
        unsorted=int(pc.sum(pc.less(diffs, 0)).as_py() or 0) if n > 1 else 0,
        dup_ts=int(pc.sum(pc.equal(diffs, 0)).as_py() or 0) if n > 1 else 0,
        bad_offset=int(pc.sum(pc.invert(pc.is_in(t["gmtoffset"], value_set=pa.array([-14400, -18000], pa.int32())))).as_py() or 0),
        wrong_year=int(pc.sum(pc.not_equal(et_years(t), year)).as_py() or 0),
        out_of_hours=int(pc.sum(pc.or_(pc.less(mins, EXT_OPEN), pc.greater(mins, EXT_CLOSE))).as_py() or 0),
        bad_ohlc=int(pc.sum(bad_ohlc).as_py() or 0),
    )
    uniq = sorted(pc.unique(days).to_pylist())
    out["days"] = uniq
    # profile of the newest n_open_days sessions in this file: bar counts + the 09:30 bar's open
    prof = {}
    for d in uniq[-(n_open_days + 2):]:      # +2: a run during a live session profiles a partial day
        mask = pc.equal(days, d)
        m_d = pc.filter(mins, mask); o_d = pc.filter(o, mask)
        reg_mask = pc.and_(pc.greater_equal(m_d, REG_OPEN), pc.less(m_d, REG_CLOSE))
        pre = int(pc.sum(pc.less(m_d, REG_OPEN)).as_py() or 0); reg = int(pc.sum(reg_mask).as_py() or 0)
        first_reg = pc.filter(o_d, pc.greater_equal(m_d, REG_OPEN))
        mm = pc.min_max(m_d).as_py()
        prof[iso_day(d)] = {"pre": pre, "reg": reg, "post": len(m_d) - pre - reg,
                            "first": f"{mm['min'] // 60:02d}:{mm['min'] % 60:02d}", "last": f"{mm['max'] // 60:02d}:{mm['max'] % 60:02d}",
                            "open_0930": first_reg[0].as_py() if len(first_reg) else None}
    out["profile"] = prof
    return out


def audit_symbol(store, root, s, n_open_days=3):
    """Refresh the per-file audit cache for changed files; drop entries for vanished files."""
    a = store.load_audit(s)
    sym_dir = os.path.join(root, "1m", s)
    years = parquet_years(sym_dir)
    files = {}
    for y in years:
        p = os.path.join(sym_dir, f"{y}.parquet")
        st = os.stat(p)
        prev = a["files"].get(str(y))
        if prev and prev.get("size") == st.st_size and prev.get("mtime_ns") == st.st_mtime_ns and "profile" in prev:
            files[str(y)] = prev
        else:
            files[str(y)] = audit_file(p, y, n_open_days)
    a["files"] = files
    a["audited_at"] = datetime.now(timezone.utc).isoformat()
    store.save_audit(s, a)
    return a


def symbol_gaps(a, sessions_ord, last_ok_ord, resettle_days):
    """-> (present set, missing_old list, missing_recent list). A session is 'recent' when it lies in the
    tail window the next pull re-asks anyway (vendor publish lag), 'old' otherwise."""
    present = set()
    for f in a["files"].values():
        present.update(f.get("days", []))
    if not present or not sessions_ord:
        return present, [], []
    lo = min(present)
    hi = max(present) if last_ok_ord is None else min(max(present), last_ok_ord)
    expected = [d for d in sessions_ord if lo <= d <= hi]
    missing = [d for d in expected if d not in present]
    recent_cut = hi - resettle_days - 3        # calendar days, generous for weekends
    return present, [d for d in missing if d < recent_cut], [d for d in missing if d >= recent_cut]


def daily_opens(symbol, dates):
    """{date: daily-bar open} for `dates`: per-ticker file, then watchlist, then market folders."""
    want = set(dates)
    cands = [os.path.join(DAILY_DIR, f"{symbol}.json"), os.path.join(DAILY_DIR, "watchlist", f"{symbol}.json"),
             os.path.join(DAILY_DIR, "watchlist", "market", f"{symbol}.US.json"), os.path.join(DAILY_DIR, "market", f"{symbol}.US.json")]
    for p in cands:
        try:
            rows = json.load(open(p))
        except Exception:
            continue
        return {r["date"]: float(r["open"]) for r in rows if r.get("date") in want and r.get("open") is not None}
    return {}


def daily_first_date(symbol):
    for p in (os.path.join(DAILY_DIR, f"{symbol}.json"), os.path.join(DAILY_DIR, "watchlist", f"{symbol}.json"),
              os.path.join(DAILY_DIR, "market", f"{symbol}.US.json"), os.path.join(DAILY_DIR, "watchlist", "market", f"{symbol}.US.json")):
        try:
            rows = json.load(open(p))
            if rows:
                return min(r["date"] for r in rows if r.get("date"))
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- per-symbol work
class Ctx:
    def __init__(self, cfg, root, store, budget, space, deadline, sessions):
        self.cfg, self.root, self.store, self.budget, self.space, self.deadline = cfg, root, store, budget, space, deadline
        self.sessions = sessions
        self.sessions_ord = [ord_day(s) for s in sessions]
        lc = last_completed_session(sessions) if sessions else None
        self.last_ok_ord = ord_day(lc) if lc else None
        self.stop_reason = None
        self._lock = threading.Lock()

    def halt(self, reason):
        with self._lock:
            if not self.stop_reason:
                self.stop_reason = reason

    def can_request(self):
        if self.stop_reason:
            return False
        if time.time() > self.deadline:
            self.halt("time"); return False
        if not self.space.ensure():
            self.halt("space"); return False
        if not self.budget.take():
            self.halt("budget"); return False
        return True


def pull_range(ctx, s, start, end, stats):
    """Walk [start, end) in window_days requests, merging each ET year as soon as it can no longer
    grow within this walk. Returns True when the whole range was requested."""
    window = int(ctx.cfg.get("window_days", 120)) * DAY
    sym_dir = os.path.join(ctx.root, "1m", s)
    pending = {}
    cur = int(start)

    def flush(years):
        for y in years:
            parts = pending.pop(y, None)
            if not parts:
                continue
            pa, _, _ = _pa()
            tbl = pa.concat_tables(parts)
            stats["added"] += merge_year(os.path.join(sym_dir, f"{y}.parquet"), tbl, ctx.space)
            stats["touched"].add(y)

    try:
        while cur < end:
            if not ctx.can_request():
                flush(list(pending)); return False
            w_end = min(cur + window, int(end))
            tbl = fetch_window(ctx.cfg, s, cur, w_end)
            stats["requests"] += 1
            for y, part in split_by_year(tbl).items():
                pending.setdefault(y, []).append(part)
            end_year = datetime.fromtimestamp(w_end + et_offset(w_end), tz=timezone.utc).year
            flush([y for y in list(pending) if y < end_year])
            _checkpoint(ctx, s)
            cur = w_end + 1
        flush(list(pending))
        return True
    except BaseException:
        flush(list(pending))
        raise


def _checkpoint(ctx, s):
    sym_dir = os.path.join(ctx.root, "1m", s)
    years = parquet_years(sym_dir)
    if not years:
        return
    _, pc, pq = _pa()
    firsts = pq.read_metadata(os.path.join(sym_dir, f"{years[0]}.parquet"))
    e = ctx.store.entry(s)
    # first/last ts from the edge files' statistics (cheap), falling back to a column read
    lo = hi = None
    try:
        lo = firsts.row_group(0).column(0).statistics.min
        last_md = pq.read_metadata(os.path.join(sym_dir, f"{years[-1]}.parquet"))
        hi = max(last_md.row_group(i).column(0).statistics.max for i in range(last_md.num_row_groups))
    except Exception:
        t = pq.read_table(os.path.join(sym_dir, f"{years[-1]}.parquet"), columns=["ts"])
        hi = pc.max(t["ts"]).as_py()
        lo = pc.min(pq.read_table(os.path.join(sym_dir, f"{years[0]}.parquet"), columns=["ts"])["ts"]).as_py()
    ctx.store.update(s, first_ts=int(lo) if e.get("first_ts") is None else min(int(e["first_ts"]), int(lo)),
                     last_ts=int(hi) if e.get("last_ts") is None else max(int(e["last_ts"]), int(hi)),
                     years=years)
    ctx.store.save()


def work_symbol(ctx, s, phases=("tail", "gaps")):
    """tail (or cold backfill) and/or the one-time gap fill. -> result dict. main() runs every symbol's
    tail before any gap pass, so new names are never starved of credits by historical re-asks."""
    cfg = ctx.cfg
    t0 = time.time()
    stats = {"added": 0, "requests": 0, "touched": set()}
    e = ctx.store.entry(s)
    now = _now()
    res = {"symbol": s, "dataset": "intraday_1m", "ok": True, "error": None, "note": None}
    try:
        if "tail" not in phases:
            complete = bool(e.get("complete"))
            if complete:
                _gap_pass(ctx, s, stats, res)
            return _finish(ctx, s, res, stats, t0, complete=complete)
        if not e.get("last_ts"):
            until = e.get("no_vendor_data_until")
            if until and until > now:
                res["note"] = f"no vendor data (recheck after {_iso(until)[:10]})"
                return _finish(ctx, s, res, stats, t0, complete=False)
            # probe the newest 10 days before walking 22 years of windows
            if not ctx.can_request():
                res["note"] = f"not started ({ctx.stop_reason})"; return _finish(ctx, s, res, stats, t0, complete=False)
            probe = fetch_window(cfg, s, now - 10 * DAY, now)
            stats["requests"] += 1
            if probe.num_rows == 0:
                retry = int(cfg.get("no_data_retry_days", 7))
                ctx.store.update(s, no_vendor_data_until=now + retry * DAY, complete=False)
                res["note"] = "no vendor data for the last 10 days — not backfilled"
                return _finish(ctx, s, res, stats, t0, complete=False)
            hint = daily_first_date(s)
            start = datetime.strptime(cfg.get("from", "2004-01-02"), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
            if hint:
                start = max(start, datetime.strptime(hint, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() - 7 * DAY)
        else:
            start = int(e["last_ts"]) - int(cfg.get("resettle_days", 4)) * DAY
        complete = pull_range(ctx, s, start, now, stats)
        ctx.store.update(s, complete=complete, no_vendor_data_until=None)
        if complete and "gaps" in phases:
            _gap_pass(ctx, s, stats, res)
        return _finish(ctx, s, res, stats, t0, complete=complete)
    except TickerNotFound as ex:
        res.update(ok=False, error=f"ticker not found at vendor ({vendor_symbol(cfg, s)}.US): {ex}")
    except CreditsExhausted as ex:
        ctx.budget.stop(); ctx.halt("budget"); res["note"] = str(ex)
    except NeedSpace as ex:
        ctx.halt("space"); res.update(ok=False, error=f"NEED_SPACE: {ex}")
    except Exception as ex:
        res.update(ok=False, error=f"{type(ex).__name__}: {str(ex)[:160]}")
    return _finish(ctx, s, res, stats, t0, complete=False)


def _gap_pass(ctx, s, stats, res):
    """One-time gap fill, only for a caught-up symbol (a partial backfill is not a gap)."""
    cfg = ctx.cfg
    if not cfg.get("gap_fill", True) or not ctx.sessions_ord:
        return
    a = audit_symbol(ctx.store, ctx.root, s, int(cfg.get("verify_sessions", 3)))
    _, old, _ = symbol_gaps(a, ctx.sessions_ord, ctx.last_ok_ord, int(cfg.get("resettle_days", 4)))
    tried = set(a.get("tried_days", []))
    todo = [d for d in old if d not in tried]
    if not todo:
        return
    win_days = int(cfg.get("window_days", 120))
    groups, g = [], [todo[0]]
    for d in todo[1:]:
        if d - g[0] < win_days - 4:      # the request pads a day each side
            g.append(d)
        else:
            groups.append(g); g = [d]
    groups.append(g)
    filled_groups = 0
    for g in groups:
        before = stats["requests"]
        if not pull_range(ctx, s, g[0] * DAY - DAY, (g[-1] + 2) * DAY, stats):
            break
        if stats["requests"] > before:
            a = ctx.store.load_audit(s)
            a["tried_days"] = sorted(set(a.get("tried_days", [])) | set(g))
            ctx.store.save_audit(s, a); filled_groups += 1
    res["gap_windows"] = filled_groups
    res["gap_sessions"] = len(todo)


def _finish(ctx, s, res, stats, t0, complete):
    sym_dir = os.path.join(ctx.root, "1m", s)
    years = parquet_years(sym_dir)
    if years:
        try:
            _checkpoint(ctx, s)
        except Exception:
            pass
    e = ctx.store.entry(s)
    rows = sum(year_rows(os.path.join(sym_dir, f"{y}.parquet")) for y in years)
    ctx.store.update(s, rows=rows, years=years, updated_at=datetime.now(timezone.utc).isoformat(),
                     requests_total=int(e.get("requests_total", 0)) + stats["requests"])
    ctx.store.save(force=True)
    res.update(count=rows, added=stats["added"], requests=stats["requests"], complete=bool(ctx.store.entry(s).get("complete")),
               years_touched=sorted(stats["touched"]), seconds=round(time.time() - t0, 1))
    return res


# ---------------------------------------------------------------- verification
def verify(ctx, symbols):
    """Full-history check of every configured symbol. Uses the audit cache, so only files that changed
    since the last verification are read in full."""
    cfg = ctx.cfg
    n_open = int(cfg.get("verify_sessions", 3)); resettle = int(cfg.get("resettle_days", 4))
    last_ok = iso_day(ctx.last_ok_ord) if ctx.last_ok_ord is not None else None
    per = {}

    def one(s):
        e = ctx.store.entry(s)
        a = audit_symbol(ctx.store, ctx.root, s, n_open)
        if not a["files"]:
            return s, {"status": "no vendor data" if e.get("no_vendor_data_until") else "no data"}
        present, old, recent = symbol_gaps(a, ctx.sessions_ord, ctx.last_ok_ord, resettle)
        tried = set(a.get("tried_days", []))
        struct = {k: sum(int(f.get(k, 0)) for f in a["files"].values()) for k in ("unsorted", "dup_ts", "bad_offset", "wrong_year", "out_of_hours", "bad_ohlc")}
        prof = {}
        for y in sorted(a["files"], key=int)[-2:]:        # a January run's last closed session can sit in last year's file
            prof.update(a["files"][y].get("profile", {}))
        if last_ok:
            prof = {d: v for d, v in prof.items() if d <= last_ok}
        last_day = max(prof) if prof else None
        lp = prof.get(last_day, {}) if last_day else {}
        checks = []
        days_chk = sorted(prof)[-n_open:]
        dopen = daily_opens(s, days_chk)
        for d in days_chk:
            o930 = prof[d].get("open_0930"); do = dopen.get(d)
            if o930 and do:
                checks.append({"date": d, "bar_0930_open": o930, "daily_open": do, "diff_bps": round(abs(o930 - do) / do * 1e4, 2)})
        return s, {
            "status": "ok", "complete": bool(e.get("complete")), "years": sorted(int(y) for y in a["files"]),
            "rows_total": sum(int(f["rows"]) for f in a["files"].values()),
            "first_session": iso_day(min(present)), "last_session": iso_day(max(present)),
            "sessions_present": len(present),
            "missing_sessions_old": len(old), "missing_old_untried": len([d for d in old if d not in tried]),
            "missing_old_sample": [iso_day(d) for d in old[-5:]],
            "missing_recent": [iso_day(d) for d in recent],
            "lag_sessions": len([d for d in ctx.sessions_ord if max(present) < d <= (ctx.last_ok_ord or 0)]),
            "structure": struct,
            "last_session_bars": {"date": last_day, **lp},
            "open_check": checks,
        }

    def safe(s):
        try:
            return one(s)
        except Exception as ex:
            return s, {"status": "error", "error": f"{type(ex).__name__}: {str(ex)[:160]}"}

    with ThreadPoolExecutor(max_workers=int(cfg.get("workers", 8))) as ex:
        for s, v in ex.map(safe, symbols):
            per[s] = v
    ok = {s: v for s, v in per.items() if v.get("status") == "ok"}
    errored = sorted(s for s, v in per.items() if v.get("status") == "error")
    no_vendor = sorted(s for s, v in per.items() if v.get("status") == "no vendor data")
    no_data = sorted(s for s, v in per.items() if v.get("status") == "no data")
    incomplete = sorted(s for s, v in ok.items() if not v["complete"])
    struct_bad = sorted(s for s, v in ok.items() if any(v["structure"][k] for k in ("unsorted", "dup_ts", "bad_offset", "wrong_year")))
    fresh = [v for v in ok.values() if v["lag_sessions"] == 0]
    ext = sum(1 for v in fresh if (v["last_session_bars"].get("pre") or 0) + (v["last_session_bars"].get("post") or 0) > 0)
    opens = [c for v in ok.values() for c in v["open_check"]]
    worst = max((c["diff_bps"] for c in opens), default=0.0)
    open_bad = sorted({s for s, v in ok.items() for c in v["open_check"] if c["diff_bps"] > 10})
    untried = sum(v["missing_old_untried"] for v in ok.values())
    problems = []
    if errored: problems.append(f"unreadable store for {len(errored)} symbols: {[(s, per[s]['error']) for s in errored[:5]]}")
    if struct_bad: problems.append(f"structural errors in {len(struct_bad)} symbols: {struct_bad[:10]}")
    if open_bad: problems.append(f"09:30 bar open > 10 bps from the daily open in {len(open_bad)} symbols: {open_bad[:10]}")
    if fresh and ext < 0.95 * len(fresh): problems.append(f"extended-hours bars on only {ext}/{len(fresh)} fresh symbols")
    pending = []
    if no_data: pending.append(f"{len(no_data)} symbols not pulled yet")
    if incomplete: pending.append(f"{len(incomplete)} symbols not caught up to now (backfill or top-up pending)")
    if untried: pending.append(f"{untried} historical sessions not yet re-asked")
    status = "failed" if problems else ("incomplete" if pending else "verified")
    summary = {
        "status": status, "last_completed_session": last_ok,
        "symbols_configured": len(symbols), "symbols_with_data": len(ok), "symbols_complete": len(ok) - len(incomplete),
        "symbols_not_pulled": no_data[:50], "symbols_no_vendor_data": no_vendor,
        "symbols_partial": incomplete[:50],
        "rows_total": sum(v["rows_total"] for v in ok.values()),
        "symbols_fresh": len(fresh),
        "symbols_lagging": sorted((s for s, v in ok.items() if v["lag_sessions"]), key=lambda s: -ok[s]["lag_sessions"])[:50],
        "missing_sessions_old": sum(v["missing_sessions_old"] for v in ok.values()),
        "missing_sessions_old_untried": untried,
        "symbols_with_old_gaps": sum(1 for v in ok.values() if v["missing_sessions_old"]),
        "structural_error_symbols": struct_bad,
        "out_of_hours_bars": sum(v["structure"]["out_of_hours"] for v in ok.values()),
        "ohlc_anomaly_bars": sum(v["structure"]["bad_ohlc"] for v in ok.values()),
        "symbols_with_extended_hours_on_last_session": ext,
        "open_checks": len(opens), "open_checks_within_5bps": sum(1 for c in opens if c["diff_bps"] <= 5),
        "open_check_worst_bps": round(worst, 2),
        "problems": problems, "pending": pending,
    }
    out = {"generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "symbols": per}
    tmp = os.path.join(ctx.root, "_verify.json.tmp")
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, os.path.join(ctx.root, "_verify.json"))
    return out


# ---------------------------------------------------------------- main
def resolve_root():
    """FOLDER RULE. Returns the directory to write into (created)."""
    marker = os.path.join(DATA_DIR, "_manifest.json")
    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
        log(f"root: {DATA_DIR} (created)")
        return DATA_DIR
    try:
        if json.load(open(marker)).get("producer") == PRODUCER:
            log(f"root: {DATA_DIR} (ours, adding to it)")
            return DATA_DIR
    except Exception:
        pass
    alt = ALT_DATA_DIR
    alt_marker = os.path.join(alt, "_manifest.json")
    if os.path.isdir(alt):
        try:
            if json.load(open(alt_marker)).get("producer") != PRODUCER:
                alt = alt.rstrip("/") + "_" + datetime.now(timezone.utc).strftime("%Y%m%d")
        except Exception:
            alt = alt.rstrip("/") + "_" + datetime.now(timezone.utc).strftime("%Y%m%d")
    os.makedirs(alt, exist_ok=True)
    log(f"root: {DATA_DIR} exists and is not this fetcher's — using {alt}")
    return alt


def resolve_universe(cfg, config_dir):
    """This config's stocks first (the research names keep their nightly slot), then every stock in
    universe_configs, then market symbols (".US" stripped). Order-preserving union."""
    out = list(cfg.get("stocks", []))
    market = list(cfg.get("market", []))
    for name in cfg.get("universe_configs", []):
        p = os.path.join(config_dir, name)
        try:
            u = json.load(open(p))
        except Exception as e:
            raise RuntimeError(f"universe config {p} unreadable: {e}")
        out += list(u.get("stocks", []))
        market += list(u.get("market", []))
    strip = lambda x: x[:-3] if x.endswith(".US") else x
    return list(dict.fromkeys([strip(x) for x in out] + [strip(x) for x in market]))


def main():
    if not TOKEN:
        print("FATAL: EODHD_API_TOKEN not set", file=sys.stderr); return 1
    cfg = json.load(open(CONFIG_PATH))
    symbols = resolve_universe(cfg, os.path.dirname(os.path.abspath(CONFIG_PATH)))
    root = resolve_root()
    workers = _cfg(cfg, "workers", "WORKERS", 8, int)
    log(f"intraday 1m pull: {len(symbols)} symbols, from {cfg.get('from')}, window {cfg.get('window_days', 120)}d, "
        f"{workers} workers, root {root}")
    try:
        import pyarrow  # noqa: F401
    except Exception:
        log("FATAL: pyarrow missing (bootstrap installs PIP_PACKAGES=pyarrow)"); _persist_log(root, "error"); return 1

    store = Store(root)
    reserve = _cfg(cfg, "reserve_credits", "RESERVE_CREDITS", 25000, int)
    max_req = _cfg(cfg, "max_requests_per_run", "MAX_REQUESTS", 30000, int)
    used, cap = credit_status()
    if used is not None and cap:
        affordable = max(0, (cap - reserve - used) // CREDITS_PER_REQUEST)
        log(f"     credits: {used:,}/{cap:,} used today, reserve {reserve:,} -> {affordable:,} requests affordable; run cap {max_req:,}")
        max_req = min(max_req, affordable)
    budget = Budget(max_req, reserve)
    space = Space(root, _cfg(cfg, "min_free_gb", "MIN_FREE_GB", 3.0, float), _cfg(cfg, "grow_step_gb", "GROW_STEP_GB", 1, int),
                  _cfg(cfg, "max_grow_gb_per_run", "MAX_GROW_GB", 30, int))
    deadline = time.time() + 60 * _cfg(cfg, "max_run_minutes", "MAX_RUN_MINUTES", 430, float)
    ctx = Ctx(cfg, root, store, budget, space, deadline, load_sessions())
    log(f"     last completed XNYS session: {iso_day(ctx.last_ok_ord) if ctx.last_ok_ord is not None else '?'}")

    warm = [s for s in symbols if store.entry(s).get("last_ts")]
    cold = [s for s in symbols if not store.entry(s).get("last_ts")]
    order = warm + cold
    log(f"     {len(warm)} symbols to top up, {len(cold)} to backfill")
    results = []
    done = [0]
    lock = threading.Lock()
    t_start = time.time()

    def run(s, phases=("tail",), tag=""):
        r = work_symbol(ctx, s, phases)
        with lock:
            done[0] += 1; i = done[0]; results.append(r)
        e = store.entry(s)
        log(f"[{tag}{i:3d}/{len(order)}] {s:6s} +{r.get('added', 0):,} rows in {r.get('requests', 0)} req ({r.get('seconds', 0):.0f}s) "
            f"{'complete' if r.get('complete') else 'partial'} rows={e.get('rows')} last={_iso(e.get('last_ts'))}"
            + (f" gaps={r['gap_windows']}w" if r.get("gap_windows") else "")
            + (f"  ({r['note']})" if r.get("note") else "") + (f"  !! {r['error']}" if r.get("error") else ""))
        if i % 25 == 0:
            log(f"     .. {i}/{len(order)} symbols, {budget.used:,} requests, budget left {budget.left:,}, "
                f"free {space.free_gb():.1f} GB, grown {space.grown} GB, {(time.time() - t_start) / 60:.0f} min")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(run, order))
    store.save(force=True)
    if not ctx.stop_reason and cfg.get("gap_fill", True):
        tails = {r["symbol"]: r for r in results}
        caught_up = [s for s in order if store.entry(s).get("complete")]
        log(f"     gap pass: {len(caught_up)} caught-up symbols — re-asking historical sessions with no bars, once each")
        done[0] = 0
        results_tail, results[:] = list(results), []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda s: run(s, ("gaps",), "gap "), caught_up))
        store.save(force=True)
        for g in results:                          # fold gap work into the symbol's single result row
            t = tails.get(g["symbol"])
            if t is None:
                continue
            t["added"] += g.get("added", 0); t["requests"] += g.get("requests", 0)
            t["gap_windows"] = g.get("gap_windows"); t["gap_sessions"] = g.get("gap_sessions")
            if not g["ok"]:
                t["ok"], t["error"] = False, g["error"]
        results[:] = results_tail

    need_space = ctx.stop_reason == "space"
    all_ok = all(r["ok"] for r in results if not (r.get("error") or "").startswith("NEED_SPACE"))
    _write_run(root, cfg, results, ctx, budget, space, len(symbols))
    try:
        v = verify(ctx, symbols)
        sm = v["summary"]
        log(f"verify: {json.dumps({k: sm[k] for k in ('status', 'symbols_configured', 'symbols_with_data', 'symbols_complete', 'rows_total', 'symbols_fresh', 'missing_sessions_old', 'missing_sessions_old_untried', 'symbols_with_extended_hours_on_last_session', 'open_checks', 'open_checks_within_5bps', 'open_check_worst_bps')})}")
        for p in sm["problems"]:
            log(f"verify PROBLEM: {p}")
        for p in sm["pending"]:
            log(f"verify pending: {p}")
        if sm["symbols_no_vendor_data"]:
            log(f"verify note: no vendor intraday data for {sm['symbols_no_vendor_data']}")
        if sm["status"] == "failed":
            all_ok = False
    except Exception as e:
        log(f"verify failed: {type(e).__name__}: {e}"); log(traceback.format_exc()); all_ok = False
    if need_space:
        log(f"NEED_SPACE: {space.free_gb():.2f} GB free — could not grow further in-pod; host runner grows 1 GB and relaunches")
        _persist_log(root, "error"); return EXIT_NEED_SPACE
    if not all_ok:
        log(f"FAILED — error log: {_persist_log(root, 'error')}"); return 1
    if STORE_LOGS:
        log(f"run log: {_persist_log(root, 'run')}")
    return 0


def _iso(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if ts else "-"


def _write_run(root, cfg, results, ctx, budget, space, n_configured):
    m = {"vendor": "eodhd", "dataset": "intraday_1m", "producer": PRODUCER, "ended_at": datetime.now(timezone.utc).isoformat(),
         "config": {k: cfg.get(k) for k in ("from", "window_days", "max_requests_per_run", "reserve_credits", "resettle_days",
                                            "min_free_gb", "grow_step_gb", "workers", "max_run_minutes")},
         "n_configured": n_configured, "n_symbols": len(results),
         "n_ok": sum(1 for r in results if r["ok"]), "n_fail": sum(1 for r in results if not r["ok"]),
         "requests_used": sum(r.get("requests", 0) for r in results), "rows_added": sum(r.get("added", 0) for r in results),
         "stop_reason": ctx.stop_reason,
         "budget_exhausted": ctx.stop_reason == "budget", "time_capped": ctx.stop_reason == "time",
         "need_space": ctx.stop_reason == "space",
         "space_mode": space.mode, "volume_grown_gb": space.grown, "free_gb_at_end": round(space.free_gb(), 2),
         "results": sorted(results, key=lambda r: r["symbol"])}
    with open(os.path.join(root, "_run.json"), "w") as f:
        json.dump(m, f, indent=2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            _LOG_LINES.append(traceback.format_exc()); _persist_log(DATA_DIR if os.path.isdir(DATA_DIR) else ".", "crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
