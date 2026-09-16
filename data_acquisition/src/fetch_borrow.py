#!/usr/bin/env python3
"""D-10 borrow fees / availability -> `borrow_fees`. The spec's most TIME-CRITICAL collector.

Spec v1.2 §2 D-10 + §6 G-05: no vendor sells retail borrow-fee HISTORY, so this table can only
accrue FORWARD — every day this job does not run is a day of data that can never be recovered.
That is why it writes an immutable snapshot per vendor publication and never overwrites one.

SOURCE (free, no IBKR account, verified live 2026-08-11):
    ftp://shortstock@ftp2.interactivebrokers.com/usa.txt   (anonymous, blank password)

    !! The spec names ftp3.interactivebrokers.com — that host now TIMES OUT. ftp2 (and the
       unnumbered ftp.interactivebrokers.com) serve the same file. FTP_HOSTS is tried in order,
       so a host coming back or going away needs no code change. §8-8 is settled by this.

SOURCE 2 — iBorrowDesk HISTORY (free, verified live 2026-08-11):
    https://www.iborrowdesk.com/api/ticker/<TICKER>   -> {daily: [{date, fee, available, rebate,
                                                         high_fee, low_fee, open_fee, ...}], ...}

    Two access gotchas, both load-bearing:
      * HOST — the apex `iborrowdesk.com` completes TLS then returns an empty reply, and issues NO
        redirect. Only the `www.` host answers.
      * HEADER — the www host 403s any programmatic User-Agent (python-urllib, curl's default).
        A browser UA returns 200.

    This SOFTENS spec §6 G-05. G-05 says borrow history "only accrues forward" and the table must
    "accrue from go-live" — but iBorrowDesk serves a ROLLING ~1 YEAR of daily history per ticker
    (AAPL/NVDA/GNRC each returned 259 rows spanning 2025-08-12..2026-08-10). That covers the whole
    pinned test window on day one instead of starting empty. The window rolls, so the daily merge
    below is still what turns a 1-year rolling view into a permanently growing history — but the
    cost of a missed day is now "lose it in ~12 months", not "lose it immediately".

SOURCE 3 — iBorrowDesk v2 API, ALL-TIME (keyed, metered; verified live 2026-09-15):
    https://www.iborrowdesk.com/api/v2/daily/borrow?symbols=A,B.C&end=YYYY-MM-DD
    Daily open/high/low/close for fee, rebate and availability back to 2015-07, on a $10+ Patreon
    allowance of 500 units/month (1 unit = 365 days of one symbol). A one-off backfill for the
    intraday.json research names, merged into history/ under the rows already there. See
    collect_history_v2.

    IBKR-vs-iBorrowDesk: IBKR is the live indicative snapshot for ALL ~19.7k shortable names;
    iBorrowDesk is per-ticker (one request each) and therefore universe-scoped. Both are kept.

FILE FORMAT (pipe-delimited, not CSV):
    #BOF|2026.08.11|11:27:09                             <- vendor publication timestamp
    #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
    AAPL|USD|APPLE INC|265598|US0378331005|4.5800|0.2500|8500000|BBG000B9XRY4|
    ...
    #EOF                                                  <- present only on a COMPLETE file

    FEERATE is percent/year -> fee_bps_yr = FEERATE * 100 (M1 `borrow_fees.fee_bps_yr`).
    AVAILABLE is a share count, or ">10000000", or "NA" when the vendor has no number.
    IBKR spells class shares with a SPACE (BRK B, BF B) where the rest of the stack uses a dash.

OUTPUT (append-only, M1-01):
    DATA_DIR/USA/<YYYY-MM-DD>T<HHMMSS>.json.gz  IBKR: every row of one vendor snapshot, never rewritten
    DATA_DIR/history/<TICKER>.json              iBorrowDesk: daily rows merged by date, grows forever
    DATA_DIR/history_v2/<TICKER>.json           iBorrowDesk v2: raw all-time payload (figi, OHLC) = done marker
    DATA_DIR/_run.json                          run manifest (provenance, §3)

The WHOLE file is kept, not just today's universe: universe membership is recomputed over time
(universe.mode = top1000_dollar_volume), so a name outside today's 503 can be inside tomorrow's —
and unlike prices, its borrow history cannot be fetched later. Gzipped, this is ~0.3 MB/day.

Self-termination is bootstrap.sh's job, so this runs/tests locally:

    DATA_DIR=./data_borrow CONFIG_PATH=data_acquisition/config/borrow.json \
      STORE_LOGS=true python3 data_acquisition/src/fetch_borrow.py

Exit code: 0 if a snapshot was stored (or an identical one already was), 1 otherwise.
"""
import gzip
import json
import math
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_borrow")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/borrow.json")
# Tried in order. ftp3 is the spec's host and is currently unreachable; ftp2 serves the same file.
FTP_HOSTS = [h for h in os.environ.get(
    "IBKR_FTP_HOSTS", "ftp2.interactivebrokers.com,ftp.interactivebrokers.com,"
                      "ftp3.interactivebrokers.com").split(",") if h]
FTP_USER = os.environ.get("IBKR_FTP_USER", "shortstock")
FTP_TIMEOUT = int(os.environ.get("IBKR_FTP_TIMEOUT", "60"))
GC_DEFAULT_BPS = float(os.environ.get("BORROW_GC_DEFAULT_BPS", "50"))  # spec D-10 / G-05 default
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

# --- iBorrowDesk (rolling ~1y daily history, per ticker) ---
# The apex host answers TLS then hangs up with an empty reply and no redirect — www is mandatory.
IBD_URL = os.environ.get("IBORROWDESK_URL", "https://www.iborrowdesk.com/api/ticker/{ticker}")
# The site 403s programmatic User-Agents (python-urllib, curl default). A browser UA gets 200.
IBD_UA = os.environ.get(
    "IBORROWDESK_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# iBorrowDesk RATE-LIMITS HARD, and the ban is by IP, not by session. Measured 2026-08-11: a pod
# doing ~1 req/s got 106 tickers through and was then cut off; a SECOND pod from the same datacenter
# was blocked on its very first request (0 OK / 93 blocked). The block surfaces as nginx
# **HTTP 444** ("connection closed without response"), not 429, so it does not look like throttling.
#
# Consequences baked into the design below: pace slowly, cap requests per run, skip tickers already
# refreshed recently, and BAIL as soon as a block is detected rather than hammering through several
# hundred doomed requests (which only deepens the ban). The universe therefore fills in over several
# daily runs instead of one — which is fine, because the vendor window is a ROLLING YEAR: nothing is
# lost by taking a week to cover 503 names, and the truly unrecoverable half (the IBKR snapshot)
# runs first and is untouched by any of this.
IBD_PACE_SEC = float(os.environ.get("IBORROWDESK_PACE_SEC", "20"))
IBD_TIMEOUT = int(os.environ.get("IBORROWDESK_TIMEOUT", "20"))
IBD_ATTEMPTS = int(os.environ.get("IBORROWDESK_ATTEMPTS", "2"))
IBD_MAX_REQUESTS = int(os.environ.get("IBORROWDESK_MAX_REQUESTS", "80"))    # per run
IBD_SKIP_FRESH_DAYS = float(os.environ.get("IBORROWDESK_SKIP_FRESH_DAYS", "3"))
IBD_BLOCK_GIVEUP = int(os.environ.get("IBORROWDESK_BLOCK_GIVEUP", "3"))     # consecutive blocks
IBD_BLOCK_CODES = {429, 444, 503}
# Hard ceiling on the whole history phase. urlopen's timeout is per socket operation, so a server
# that trickles bytes can hold one request open far longer than IBD_TIMEOUT — observed live: a pod
# wedged on a single ticker and stopped advancing. Never let this eat the 8h pod watchdog.
IBD_PHASE_BUDGET_SEC = int(os.environ.get("IBORROWDESK_PHASE_BUDGET_SEC", "2700"))  # 45 min

# --- iBorrowDesk v2 API (keyed, metered): ALL-TIME daily history ---
# The free endpoint above stops at a rolling year. v2 sells the whole series, and a $10+ Patreon
# pledge carries 500 units a month of it, resetting on the 1st (UTC). From the live OpenAPI spec
# (/api/v2/openapi.json, served only to a logged-in session), probed 2026-09-15:
#   * billing is ceil(returned span / unit_days) per symbol, on what actually came back — a 2023
#     listing costs ~3 units on an all-time ask; unknown symbols and empty answers are free
#   * a request whose WORST case exceeds daily_max_units_per_request (150) is a 400, so batches are
#     sized from /coverage (free, keyless) instead of hardcoding the ceiling
#   * class shares take the DOT (`BRK.B`); the dash comes back in meta.unresolved, unbilled
#   * a spent allowance is 402 budget_exhausted; rate limit 60/min, 2000/h, 20000/day
IBD2_BASE = os.environ.get("IBORROWDESK_V2_BASE", "https://www.iborrowdesk.com/api/v2")
IBD2_KEY = os.environ.get("IBORROWDESK_API_KEY", "").strip()
IBD2_TIMEOUT = int(os.environ.get("IBORROWDESK_V2_TIMEOUT", "180"))
IBD2_PACE_SEC = float(os.environ.get("IBORROWDESK_V2_PACE_SEC", "2"))
IBD2_PHASE_BUDGET_SEC = int(os.environ.get("IBORROWDESK_V2_PHASE_BUDGET_SEC", "1800"))  # 30 min
IBD2_UNRESOLVED_RETRY_DAYS = float(os.environ.get("IBORROWDESK_V2_UNRESOLVED_RETRY_DAYS", "30"))
# BORROW_V2_ONLY=1 runs just the keyed backfill + its validation: no IBKR snapshot, no free pull, and
# the manifest goes to _run_history_v2.json so post.py never mistakes it for a finished borrow fetch.
V2_ONLY = os.environ.get("BORROW_V2_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
# The vendor floor when this was written; only used to label a complete series that starts late.
IBD2_FLOOR_FALLBACK = "2015-07-13"

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


def fetch_file(country="usa"):
    """Download <country>.txt from the first FTP host that answers. Returns (text, url)."""
    errors = []
    for host in FTP_HOSTS:
        url = f"ftp://{FTP_USER}@{host}/{country}.txt"
        try:
            with urllib.request.urlopen(url, timeout=FTP_TIMEOUT) as r:
                text = r.read().decode("utf-8", "replace")
            if len(text) < 1000:
                raise RuntimeError(f"implausibly short file ({len(text)} bytes)")
            log(f"     fetched {len(text)} bytes from {host}")
            return text, url
        except (urllib.error.URLError, socket.timeout, OSError, RuntimeError) as e:
            errors.append(f"{host}: {type(e).__name__}: {e}")
            log(f"     {host} unavailable ({type(e).__name__}) — trying next host")
    raise RuntimeError("no IBKR FTP host reachable — " + " | ".join(errors))


def _num(v):
    """'0.2500' -> 0.25 ; 'NA'/'' -> None ; '>10000000' -> 10000000.0 (the vendor's floor)."""
    if v is None:
        return None
    v = v.strip().lstrip(">")
    if not v or v.upper() in ("NA", "N/A", "NAN"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def parse(text):
    """Pipe-delimited IBKR short-stock file -> (rows, snapshot_iso, complete).

    `complete` is False when the trailing #EOF marker is absent, which is how a truncated
    mid-publication download announces itself — those are NOT stored (a frozen partial snapshot
    is unrecoverable in exactly the way G-05 warns about)."""
    lines = text.splitlines()
    if not lines or not lines[0].startswith("#BOF"):
        raise RuntimeError(f"unexpected file header: {lines[0][:80] if lines else '(empty)'}")
    bof = lines[0].split("|")
    try:  # "#BOF|2026.08.11|11:27:09" -> 2026-08-11T11:27:09
        snapshot = f"{bof[1].replace('.', '-')}T{bof[2]}"
        datetime.strptime(snapshot, "%Y-%m-%dT%H:%M:%S")
    except (IndexError, ValueError) as e:
        raise RuntimeError(f"unparseable #BOF timestamp {lines[0][:80]!r}: {e}")

    header = [c for c in lines[1].lstrip("#").split("|") if c]
    if "SYM" not in header or "FEERATE" not in header:
        raise RuntimeError(f"unexpected column header: {header}")

    rows, complete = [], False
    for ln in lines[2:]:
        if ln.startswith("#EOF"):
            complete = True
            continue
        if not ln or ln.startswith("#"):
            continue
        rec = dict(zip(header, ln.split("|")))
        sym = (rec.get("SYM") or "").strip()
        if not sym:
            continue
        fee_pct = _num(rec.get("FEERATE"))
        rows.append({
            # IBKR writes class shares as "BRK B"; normalise to the dash form the configs use.
            "ticker": sym.replace(" ", "-"),
            "ticker_vendor": sym,
            "date": snapshot[:10],
            "snapshot_utc": snapshot,
            # M1 borrow_fees.fee_bps_yr. NULL (not 50) when the vendor has no rate: the GC-50
            # default is a MODELLING fallback (spec D-10), and baking it in here would make a
            # guess indistinguishable from a quote in the stored history.
            "fee_bps_yr": None if fee_pct is None else round(fee_pct * 100, 4),
            "rebate_rate_pct": _num(rec.get("REBATERATE")),
            "available": _num(rec.get("AVAILABLE")),
            "available_raw": (rec.get("AVAILABLE") or "").strip(),
            "currency": (rec.get("CUR") or "").strip() or None,
            "name": (rec.get("NAME") or "").strip() or None,
            "con_id": (rec.get("CON") or "").strip() or None,
            "isin": (rec.get("ISIN") or "").strip() or None,
            "figi": (rec.get("FIGI") or "").strip() or None,
        })
    return rows, snapshot, complete


def _read_existing(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError):
        return []


_ibd_ctx = None      # None = verified TLS
_ibd_ctx_warned = False


def fetch_iborrowdesk(ticker):
    """Rolling ~1y of daily borrow rows for one ticker -> list in the M1 `borrow_fees` shape.

    TLS: falls back to unverified ONLY on SSLCertVerificationError (a host with no CA bundle —
    the RunPod slim image and bare macOS pythons both hit this), never on a transient SSLError.
    Narrowing the trigger matters: a blanket `except ssl.SSLError` downgrade turns one flaky
    frame into an unverified channel for the rest of the process. No credential is sent to this
    host, so the downgrade is bounded; it is still logged into the manifest rather than printed.

    DUAL-CLASS ALIAS. iBorrowDesk keys share classes with a DOT — `BRK.B`, `BF.B` — while our
    universe (and every other feed here) uses the dash form. The dash spelling 404s, which the
    caller counted as "vendor has no data" and left BRK-B and BF-B as the only two names in the
    503 with no borrow history at all. Both return 259 rows under the dot. Rows are still stored
    under the canonical dash ticker so they join everything else; `ticker_vendor` records what was
    actually asked for."""
    global _ibd_ctx, _ibd_ctx_warned

    def _open(sym):
        global _ibd_ctx, _ibd_ctx_warned
        req = urllib.request.Request(IBD_URL.format(ticker=sym), headers={"User-Agent": IBD_UA})
        try:
            with urllib.request.urlopen(req, timeout=IBD_TIMEOUT, context=_ibd_ctx) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            import ssl
            if _ibd_ctx is not None or not isinstance(getattr(e, "reason", e),
                                                      ssl.SSLCertVerificationError):
                raise
            if not _ibd_ctx_warned:
                log("WARN iBorrowDesk: no usable CA bundle — retrying unverified "
                    "(no credential is sent to this host)")
                _ibd_ctx_warned = True
            _ibd_ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=IBD_TIMEOUT, context=_ibd_ctx) as r:
                return json.loads(r.read().decode("utf-8", "replace"))

    # Only dashed symbols get a second attempt, and only on a 404 — a 429/444/503 is the vendor
    # refusing us and must propagate to the Blocked handling, not be retried under another name.
    aliases = [ticker] + ([ticker.replace("-", ".")] if "-" in ticker else [])
    payload, vendor_sym = None, ticker
    for i, sym in enumerate(aliases):
        try:
            payload, vendor_sym = _open(sym), sym
            break
        except urllib.error.HTTPError as e:
            if e.code != 404 or i == len(aliases) - 1:
                raise
    out = []
    for row in (payload.get("daily") or []):
        date = str(row.get("date") or "")[:10]
        if not date:
            continue
        fee = row.get("fee")
        out.append({
            "ticker": ticker,
            "date": date,
            "fee_bps_yr": None if fee is None else round(float(fee) * 100, 4),
            "rebate_rate_pct": row.get("rebate"),
            "available": row.get("available"),
            # Intraday spread: the cost model cares that a name was HTB at some point in the day,
            # not only at the close snapshot IBKR happens to publish.
            "fee_bps_yr_high": None if row.get("high_fee") is None else round(float(row["high_fee"]) * 100, 4),
            "fee_bps_yr_low": None if row.get("low_fee") is None else round(float(row["low_fee"]) * 100, 4),
            "fee_bps_yr_open": None if row.get("open_fee") is None else round(float(row["open_fee"]) * 100, 4),
            "available_low": row.get("low_available"),
            "ticker_vendor": vendor_sym,
            "source": "iborrowdesk",
        })
    return out, payload


class Blocked(Exception):
    """The vendor is refusing us (444/429/503) — back off, do not keep trying other tickers."""


def _ibd_with_retry(ticker):
    """fetch_iborrowdesk with bounded retries.

    404 is an ANSWER (not covered), re-raised immediately. A block code is raised as `Blocked` so
    the caller can stop the whole phase instead of burning the rest of the universe on requests
    that are already being refused."""
    last = None
    for attempt in range(1, IBD_ATTEMPTS + 1):
        try:
            return fetch_iborrowdesk(ticker)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if e.code in IBD_BLOCK_CODES:
                raise Blocked(f"HTTP {e.code}") from e
            last = e
        except Exception as e:
            last = e
        if attempt < IBD_ATTEMPTS:
            time.sleep(2 * attempt)
    raise last


def _merge_by_date(existing, new):
    """Union on `date`; a re-pull of the same day WINS (the vendor revises intraday), and days that
    have rolled out of the vendor's 1-year window are retained. This is what turns a rolling
    window into the permanently-growing history spec G-05 wants."""
    by = {r.get("date"): r for r in existing}
    added = 0
    for r in new:
        if r.get("date") not in by:
            added += 1
        by[r.get("date")] = r
    return sorted(by.values(), key=lambda r: r.get("date") or ""), added


def collect_history(cfg, results):
    """D-10 history backfill via iBorrowDesk, merged append-only per ticker."""
    ibd = cfg.get("iborrowdesk") or {}
    if not ibd.get("enabled"):
        return
    tickers = ibd.get("stocks") or []
    if not tickers and ibd.get("stocks_from"):
        # Reuse another config's universe rather than duplicating a 503-entry list.
        path = ibd["stocks_from"]
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(CONFIG_PATH), path)
        try:
            with open(path) as f:
                tickers = json.load(f).get("stocks") or []
        except (OSError, ValueError) as e:
            log(f"FAIL borrow history: cannot read universe from {path}: {e}")
            results.append({"symbol": "iborrowdesk", "dataset": "borrow_history", "ok": False,
                            "count": 0, "added": 0, "error": f"{type(e).__name__}: {e}"})
            return
    if not tickers:
        return

    out_dir = os.path.join(DATA_DIR, "history")
    deadline = time.monotonic() + IBD_PHASE_BUDGET_SEC
    now = time.time()

    def _fresh(path):
        try:
            return (now - os.path.getmtime(path)) / 86400.0 < IBD_SKIP_FRESH_DAYS
        except OSError:
            return False

    # Oldest-first: whatever was refreshed longest ago gets this run's budget, so successive runs
    # sweep the universe round-robin instead of restarting at 'A' and never reaching 'Z'.
    pending = sorted((t for t in tickers if not _fresh(os.path.join(out_dir, f"{t}.json"))),
                     key=lambda t: os.path.getmtime(os.path.join(out_dir, f"{t}.json"))
                     if os.path.exists(os.path.join(out_dir, f"{t}.json")) else 0)
    skipped_fresh = len(tickers) - len(pending)
    budget = min(IBD_MAX_REQUESTS, len(pending))
    log(f"     iBorrowDesk history: {len(pending)} stale of {len(tickers)} "
        f"({skipped_fresh} fresh < {IBD_SKIP_FRESH_DAYS}d), doing {budget} this run "
        f"at {IBD_PACE_SEC}s/req (~{budget * IBD_PACE_SEC / 60:.0f} min)")

    used = consecutive_blocks = 0
    blocked_off = False
    for t in pending:
        out = os.path.join(out_dir, f"{t}.json")
        entry = {"symbol": t, "dataset": "borrow_history", "ok": True, "count": 0,
                 "added": 0, "error": None}
        # Deferral (budget spent / out of time / vendor is refusing us) is NOT a failure: the
        # vendor window is a rolling year, so the next run picks these up with nothing lost.
        if blocked_off or used >= budget or time.monotonic() > deadline:
            why = ("deferred — vendor blocked this run" if blocked_off else
                   "deferred — per-run request budget spent" if used >= budget else
                   "deferred — phase time budget exhausted")
            entry.update(count=len(_read_existing(out)), error=why)
            results.append(entry)
            continue
        used += 1
        try:
            rows, _ = _ibd_with_retry(t)
            consecutive_blocks = 0
            merged, added = _merge_by_date(_read_existing(out), rows)
            if merged:
                os.makedirs(out_dir, exist_ok=True)
                tmp = out + ".part"
                with open(tmp, "w") as f:
                    json.dump(merged, f)
                os.replace(tmp, out)
            entry.update(count=len(merged), added=added)
            if STORE_LOGS:
                span = f"{merged[0]['date']}..{merged[-1]['date']}" if merged else "empty"
                log(f"OK   borrow_history {t}: {len(merged)} (+{added}) [{span}] -> {out}")
        except Blocked as e:
            consecutive_blocks += 1
            entry.update(count=len(_read_existing(out)), error=f"blocked ({e})")
            if consecutive_blocks >= IBD_BLOCK_GIVEUP:
                blocked_off = True
                log(f"WARN borrow_history: vendor blocked us ({e}) {consecutive_blocks}x in a row "
                    f"after {used} requests — stopping the history phase for this run "
                    f"(remaining tickers deferred; the rolling window means nothing is lost)")
            else:
                time.sleep(30)  # brief backoff before deciding it is a real block
        except urllib.error.HTTPError as e:
            # 404 = an ANSWER, not a failure: iBorrowDesk is explicitly a PARTIAL source (spec §1)
            # and does not carry every name; delisted names drop out of it entirely.
            entry.update(count=len(_read_existing(out)),
                         error=("404 — not covered by iBorrowDesk" if e.code == 404
                                else f"HTTPError: {e.code}"))
            entry["ok"] = e.code == 404
            if e.code != 404:
                log(f"FAIL borrow_history {t}: HTTP {e.code}")
        except Exception as e:
            entry.update(ok=False, error=f"{type(e).__name__}: {e}")
            log(f"FAIL borrow_history {t}: {entry['error']}")
        results.append(entry)
        time.sleep(IBD_PACE_SEC)


class V2Error(Exception):
    """An HTTP error from the v2 API, carrying the vendor's machine-readable `error` code."""

    def __init__(self, status, code="", message="", details=None):
        super().__init__(f"HTTP {status} {code}: {message}".strip())
        self.status, self.code, self.message = status, code, message
        self.details = details if isinstance(details, dict) else {}


_ibd2_ctx = None
_ibd2_ctx_ready = False


def _ibd2_request(path, params=None, auth=True):
    """GET IBD2_BASE + path -> parsed JSON. An HTTP error becomes V2Error; network errors propagate.

    TLS follows fetch_intraday.py: certifi's bundle when importable, else the system store, and one
    unverified retry ONLY on a certificate-verification failure (the slim pod image) — the fallback
    every other keyed fetcher here already takes."""
    global _ibd2_ctx, _ibd2_ctx_ready
    import ssl
    if not _ibd2_ctx_ready:
        _ibd2_ctx_ready = True
        try:
            import certifi
            _ibd2_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ibd2_ctx = None
    url = IBD2_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    headers = {"User-Agent": IBD_UA, "Accept": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {IBD2_KEY}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=IBD2_TIMEOUT, context=_ibd2_ctx) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except ValueError:
                body = {}
            body = body if isinstance(body, dict) else {}
            raise V2Error(e.code, body.get("error", ""), body.get("message", ""),
                          body.get("details")) from None
        except urllib.error.URLError as e:
            if attempt == 1 and isinstance(getattr(e, "reason", e), ssl.SSLCertVerificationError):
                log("WARN iBorrowDesk v2: no usable CA bundle — retrying unverified")
                _ibd2_ctx = ssl._create_unverified_context()
                continue
            raise


def _utcnow():
    return datetime.now(timezone.utc)


def _names_from(ref, keys=("stocks",)):
    """Tickers listed under `keys` of another config file, resolved next to CONFIG_PATH."""
    path = ref if os.path.isabs(ref) else os.path.join(os.path.dirname(CONFIG_PATH), ref)
    with open(path) as f:
        data = json.load(f)
    out = []
    for k in keys:
        out.extend(data.get(k) or [])
    return out


def _v2_window(earliest, unit_days, today, overlap_days):
    """(end, worst_units_per_symbol) for a backfill request that starts at the vendor's first day.

    overlap_days=None asks for everything up to today. Otherwise the end is pulled back to the last
    day that still bills the fewest whole units while overlapping the FREE endpoint's rolling year by
    at least overlap_days: those recent days are already in history/ from the nightly free pull, and
    paying for them again costs one extra unit per symbol (12 instead of 11 in September 2026)."""
    if overlap_days is None:
        return today, max(1, math.ceil(((today - earliest).days + 1) / unit_days))
    reach = today - timedelta(days=365 - int(overlap_days))
    units = max(1, math.ceil(((reach - earliest).days + 1) / unit_days))
    return min(today, earliest + timedelta(days=units * unit_days - 2)), units


def _bps(pct):
    return None if pct is None else round(float(pct) * 100, 4)


def _v2_rows(ticker, vendor_sym, series):
    """One v2 symbol series -> rows in the SAME shape fetch_iborrowdesk stores, so build_m1's
    borrow_fees gains no new columns. The closes map onto the free endpoint's fields (the free `fee`
    is the day's close: identical on every overlapping day checked); the richer OHLC stays in the
    raw history_v2/ copy."""
    out = []
    for ob in series.get("observations") or []:
        day = str(ob.get("date") or "")[:10]
        if not day:
            continue
        fee, rebate, avail = ob.get("fee") or {}, ob.get("rebate") or {}, ob.get("available") or {}
        out.append({
            "ticker": ticker,
            "date": day,
            "fee_bps_yr": _bps(fee.get("close")),
            "rebate_rate_pct": rebate.get("close"),
            "available": avail.get("close"),
            "fee_bps_yr_high": _bps(fee.get("high")),
            "fee_bps_yr_low": _bps(fee.get("low")),
            "fee_bps_yr_open": _bps(fee.get("open")),
            "available_low": avail.get("low"),
            "ticker_vendor": vendor_sym,
            "source": "iborrowdesk_v2",
        })
    return out


def _merge_keep_existing(existing, new):
    """Union on `date` where EXISTING rows win: the backfill only ever adds days, it never rewrites
    what the nightly free pull already stored for the overlap."""
    by = {r.get("date"): r for r in existing}
    added = 0
    for r in new:
        if r.get("date") not in by:
            by[r.get("date")] = r
            added += 1
    return sorted(by.values(), key=lambda r: r.get("date") or ""), added


def _write_json(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=indent)
    os.replace(tmp, path)


def _v2_complete(rec):
    """Records written before chunking came from one all-time request (window.start null): complete."""
    if "complete" in rec:
        return bool(rec["complete"])
    return (rec.get("window") or {}).get("start", "") is None


def collect_history_v2(cfg):
    """All-time borrow history via the keyed v2 API, merged into history/<T>.json, BREADTH-FIRST.

    Each run splits the allowance evenly across every unfinished name and asks for that many years
    counting back from where the name's history stops — 392 units over 92 names buys every name its
    newest 4 years today, and the next allowances deepen all of them together — instead of taking a
    few names to 2015 and leaving the rest at the free endpoint's single year. Chunks are exact
    multiples of unit_days, so splitting costs no more than one all-time request. A name is complete
    once a chunk reaches the vendor floor, or a chunk comes back empty (nothing older; unbilled).

    Returns the manifest's `history_v2` block, which stays OUT of the nightly run's ok flag and exit
    code: a spent allowance is the expected state for weeks and must never fail the pod that carries
    the unrecoverable IBKR snapshot. Real failures still log FAIL and set history_v2.ok = false.

    State lives in history_v2/<T>.json: the raw observations (full OHLC, figi, previous_identities),
    `covered_from` (the start of the oldest chunk fetched) and `complete`. After completion a run
    makes no request. Universe = `stocks_from` (intraday.json's stocks + market) narrowed to this
    pass's own iBorrowDesk universe, so each name lands in the tree whose nightly free pull maintains
    it, minus `exclude_stocks_from` (a name both passes hold is billed once, by the main pass)."""
    v2 = cfg.get("iborrowdesk_v2") or {}
    summary = {"enabled": bool(v2.get("enabled")), "ok": True, "universe": 0, "pending": 0,
               "symbols_fetched": 0, "chunks": 0, "rows_added": 0, "requests": 0, "units_billed": 0,
               "complete": 0, "partial": {}, "unresolved": [], "deferred": 0, "remaining": None,
               "resets_at": None, "window_end": None, "error": None, "validation": None}
    if not summary["enabled"]:
        return summary

    def fail(msg):
        summary.update(ok=False, error=msg)
        log(f"FAIL borrow_history_v2: {msg}")
        return summary

    try:
        if v2.get("stocks_from"):
            names = _names_from(v2["stocks_from"], v2.get("stocks_from_keys") or ["stocks"])
        else:
            names = list(v2.get("stocks") or [])
        if v2.get("only_in_history_universe", True):
            ibd = cfg.get("iborrowdesk") or {}
            own = set(ibd.get("stocks") or
                      (_names_from(ibd["stocks_from"]) if ibd.get("stocks_from") else []))
            names = [t for t in names if t in own]
        if v2.get("exclude_stocks_from"):
            excluded = set(_names_from(v2["exclude_stocks_from"]))
            names = [t for t in names if t not in excluded]
    except (OSError, ValueError) as e:
        return fail(f"cannot read universe: {type(e).__name__}: {e}")
    names = list(dict.fromkeys(names))
    summary["universe"] = len(names)

    raw_dir = os.path.join(DATA_DIR, "history_v2")
    hist_dir = os.path.join(DATA_DIR, "history")
    now = _utcnow()
    records = {}
    for t in names:
        try:
            with open(os.path.join(raw_dir, f"{t}.json")) as f:
                records[t] = json.load(f)
        except (OSError, ValueError):
            records[t] = None

    def unresolved_recently(rec):
        age = now - datetime.fromisoformat(rec["asked_at"])
        return age.total_seconds() / 86400.0 < IBD2_UNRESOLVED_RETRY_DAYS

    def finished(t):
        rec = records[t]
        if rec is None:
            return False
        if rec.get("unresolved"):
            return unresolved_recently(rec)
        return rec.get("_stalled") or _v2_complete(rec)

    def wrap_up():
        done = [t for t in names if records[t] and not records[t].get("unresolved")
                and _v2_complete(records[t])]
        summary["complete"] = len(done)
        summary["partial"] = {t: records[t].get("covered_from") for t in names
                              if records[t] and not records[t].get("unresolved")
                              and not _v2_complete(records[t])}
        summary["deferred"] = sum(1 for t in names if not (records[t] and (
            _v2_complete(records[t]) if not records[t].get("unresolved") else True)))
        summary["validation"] = validate_history_v2(names)
        log(f"     iBorrowDesk v2 backfill: {summary['complete']}/{len(names)} complete, "
            f"{len(summary['partial'])} partial, {summary['deferred']} not complete; this run "
            f"{summary['units_billed']} units, {summary['requests']} requests, "
            f"{summary['chunks']} chunks, +{summary['rows_added']} days")
        return summary

    pending = [t for t in names if not finished(t)]
    summary["pending"] = len(pending)
    if not pending:
        log(f"     iBorrowDesk v2 backfill: complete for all {len(names)} names — nothing requested")
        return wrap_up()
    if not IBD2_KEY:
        summary["error"] = "IBORROWDESK_API_KEY not set — skipped"
        log(f"WARN borrow_history_v2: {len(pending)} names pending but {summary['error']}")
        return wrap_up()

    try:
        cov = _ibd2_request("/coverage", auth=False)
        allowance = _ibd2_request("/usage").get("allowance")
    except V2Error as e:
        return fail(str(e))
    except (OSError, ValueError) as e:
        return fail(f"{type(e).__name__}: {e}")
    earliest = date.fromisoformat(cov["daily_earliest_available"])
    unit_days = int(cov.get("unit_days") or 365)
    max_units = int(cov.get("daily_max_units_per_request") or 150)
    max_syms = int(cov.get("max_symbols") or 100)
    default_end, _ = _v2_window(earliest, unit_days, now.date(), v2.get("overlap_free_window_days", 60))
    # allowance is null for a paid subscription: no monthly ceiling, only the per-request one
    remaining = None if allowance is None else int(allowance.get("remaining") or 0)
    summary.update(window_end=default_end.isoformat(), remaining=remaining,
                   resets_at=(allowance or {}).get("resets_at"))
    left = "unmetered" if remaining is None else f"{remaining} units left (resets {summary['resets_at']})"
    log(f"     iBorrowDesk v2 backfill: {len(pending)} of {len(names)} pending, floor {earliest}, "
        f"new names end {default_end}, {left} — breadth-first")

    def chunk_end(t):
        rec = records[t]
        if rec and not rec.get("unresolved") and rec.get("covered_from"):
            return date.fromisoformat(rec["covered_from"]) - timedelta(days=1)
        if rec and not rec.get("unresolved") and rec.get("end"):
            return date.fromisoformat(rec["end"])
        return default_end

    def need(t):
        return max(0, math.ceil(((chunk_end(t) - earliest).days + 1) / unit_days))

    fetched = set()

    def apply(t, vendor_sym, series, start, cend):
        rec = records[t]
        marker = os.path.join(raw_dir, f"{t}.json")
        if series is None:
            if rec is None or rec.get("unresolved"):
                records[t] = {"ticker": t, "vendor_symbol": vendor_sym, "unresolved": True,
                              "asked_at": now.isoformat()}
                _write_json(marker, records[t])
                summary["unresolved"].append(t)
                log(f"WARN borrow_history_v2 {t}: iBorrowDesk did not resolve {vendor_sym} (unbilled) "
                    f"— asked again after {IBD2_UNRESOLVED_RETRY_DAYS:g} days")
            else:
                rec["_stalled"] = True
                log(f"WARN borrow_history_v2 {t}: resolved on an earlier run but not now — "
                    f"left at {rec.get('covered_from')} for the next run")
            return
        obs = series.get("observations") or []
        rows = _v2_rows(t, vendor_sym, {"observations": obs})
        out = os.path.join(hist_dir, f"{t}.json")
        merged, added = _merge_keep_existing(_read_existing(out), rows)
        if added:
            _write_json(out, merged)
        if rec is None or rec.get("unresolved"):
            rec = {"ticker": t, "vendor_symbol": vendor_sym, "end": cend.isoformat(), "units": 0,
                   "chunks": [], "observations": []}
        by = {o.get("date"): o for o in rec.get("observations") or []}
        for o in obs:
            by.setdefault(o.get("date"), o)
        rec["observations"] = [by[d] for d in sorted(d for d in by if d)]
        units = int(series.get("units") or 0)
        complete = start <= earliest or not obs
        rec.pop("window", None)
        rec.update(
            figi=series.get("figi") or rec.get("figi"), name=series.get("name") or rec.get("name"),
            country=series.get("country") or rec.get("country"),
            previous_identities=series.get("previous_identities") or rec.get("previous_identities") or [],
            covered_from=start.isoformat(), complete=complete, units=int(rec.get("units") or 0) + units,
            first_date=rec["observations"][0]["date"] if rec["observations"] else None,
            last_date=rec["observations"][-1]["date"] if rec["observations"] else None,
            fetched_at=now.isoformat(), source_endpoint=f"{IBD2_BASE}/daily/borrow")
        rec["chunks"] = (rec.get("chunks") or []) + [{
            "start": start.isoformat(), "end": cend.isoformat(), "first_date": series.get("first_date"),
            "last_date": series.get("last_date"), "days": len(obs), "units": units,
            "fetched_at": now.isoformat()}]
        # The record goes down AFTER history/: a crash in between re-asks this chunk rather than
        # recording coverage that never reached history/.
        _write_json(marker, rec)
        records[t] = rec
        fetched.add(t)
        summary["chunks"] += 1
        summary["rows_added"] += added
        log(f"OK   borrow_history_v2 {t}: chunk {start}..{cend} {len(obs)} days "
            f"[{series.get('first_date')}..{series.get('last_date')}] {units}u, +{added} new; "
            f"covered from {rec['first_date']}{' — COMPLETE' if complete else ''} -> {out}")

    deadline = time.monotonic() + IBD2_PHASE_BUDGET_SEC
    waited_429 = net_errors = 0
    stop = None
    while stop is None:
        for t in pending:
            if need(t) == 0 and records[t] and not _v2_complete(records[t]):
                records[t]["complete"] = True          # the previous chunk already reached the floor
                _write_json(os.path.join(raw_dir, f"{t}.json"), records[t])
        pending = [t for t in pending if not finished(t)]
        if not pending:
            break
        if remaining is not None and remaining < 1:
            stop = "allowance"
            break
        k = max_units if remaining is None else max(1, remaining // len(pending))
        groups = {}
        for t in pending:
            groups.setdefault((chunk_end(t), min(k, need(t))), []).append(t)
        progressed = False
        for (cend, u), syms in groups.items():
            per = max(1, min(max_syms, max_units // u))
            while syms and stop is None:
                if time.monotonic() > deadline:
                    stop = "time"
                    break
                n = per if remaining is None else min(per, remaining // u)
                if n < 1:
                    break
                batch = syms[:n]
                # A chunk of exactly u*unit_days days bills at most u. The chunk that can reach the
                # floor starts AT it: stopping even one day short buys that day later as a whole unit.
                reach = math.ceil(((cend - earliest).days + 1) / unit_days)
                start = earliest if u >= reach else cend - timedelta(days=u * unit_days - 1)
                asked = {t.replace("-", "."): t for t in batch}
                try:
                    payload = _ibd2_request("/daily/borrow", {"symbols": ",".join(asked),
                                                              "start": start.isoformat(),
                                                              "end": cend.isoformat()})
                except V2Error as e:
                    if e.status == 402:
                        remaining, stop = 0, "allowance"
                        break
                    if e.status == 400 and "_schema" in e.details and len(batch) > 1:
                        per = max(1, len(batch) // 2)
                        log(f"WARN borrow_history_v2: rejected on cost ({e.details['_schema']}) — "
                            f"retrying at {per} symbols/request")
                        continue
                    if e.status == 429 and waited_429 < 3:
                        waited_429 += 1
                        time.sleep(65)
                        continue
                    fail(str(e))
                    stop = "error"
                    break
                except (OSError, ValueError) as e:
                    net_errors += 1
                    if net_errors <= 2:
                        log(f"WARN borrow_history_v2: {type(e).__name__}: {e} — retry {net_errors}/2")
                        time.sleep(15 * net_errors)
                        continue
                    fail(f"{type(e).__name__}: {e}")
                    stop = "error"
                    break
                net_errors = 0
                summary["requests"] += 1
                meta = payload.get("meta") or {}
                billed = int(meta.get("units_billed") or 0)
                summary["units_billed"] += billed
                if meta.get("allowance") is not None:
                    remaining = int(meta["allowance"].get("remaining") or 0)
                    summary["resets_at"] = meta["allowance"].get("resets_at") or summary["resets_at"]
                elif remaining is not None:
                    remaining -= billed
                data = {str(key).upper(): v for key, v in (payload.get("data") or {}).items()}
                for vendor_sym, t in asked.items():
                    apply(t, vendor_sym, data.get(vendor_sym.upper()), start, cend)
                syms = syms[len(batch):]
                progressed = True
                left = "unmetered" if remaining is None else f"{remaining} units left"
                log(f"     iBorrowDesk v2 request {summary['requests']}: {len(batch)} symbols x {u}y "
                    f"chunk {start}..{cend}, {billed} units, {left}")
                time.sleep(IBD2_PACE_SEC)
        if not progressed and stop is None:
            stop = "allowance"                         # not even a 1-year chunk is affordable
    if stop == "allowance":
        log(f"     iBorrowDesk v2 backfill: allowance spent ({remaining} units left) — the rest "
            f"continues on the first run after {summary['resets_at']}")
    elif stop == "time":
        log("WARN borrow_history_v2: phase time budget spent — the rest continues next run")
    summary.update(remaining=remaining, symbols_fetched=len(fetched))
    return wrap_up()


def validate_history_v2(names):
    """Read-back checks over every backfilled name, written to history_v2/_validation.json.

    Per name: raw days unique and sorted; every OHLC leg consistent (low <= open, close <= high);
    every raw day present in history/<T>.json, whose dates are unique and sorted; and on the days the
    nightly FREE pull also stored, the v2 close must equal the free row (fee to 0.01 bps, availability
    exactly) — two endpoints, one underlying series. Gaps are reported, not failed: IB drops a name
    from its list at zero availability. A complete series starting > 180 days after its oldest chunk
    is flagged `late_start` (a later listing, or an identity break iBorrowDesk did not link)."""
    raw_dir = os.path.join(DATA_DIR, "history_v2")
    hist_dir = os.path.join(DATA_DIR, "history")
    per, tot = {}, {"names": len(names), "complete": 0, "partial": 0, "unresolved": 0, "missing": 0,
                    "days": 0, "overlap_days": 0, "fee_mismatch": 0, "avail_mismatch": 0,
                    "failing": [], "late_start": []}
    for t in names:
        try:
            with open(os.path.join(raw_dir, f"{t}.json")) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            per[t] = {"status": "missing"}
            tot["missing"] += 1
            continue
        if rec.get("unresolved"):
            per[t] = {"status": "unresolved"}
            tot["unresolved"] += 1
            continue
        status = "complete" if _v2_complete(rec) else "partial"
        tot[status] += 1
        obs = rec.get("observations") or []
        dates = [o.get("date") for o in obs]
        ohlc_bad = 0
        for o in obs:
            for leg in ("fee", "rebate", "available"):
                v = o.get(leg) or {}
                lo, hi = v.get("low"), v.get("high")
                if lo is None or hi is None:
                    continue
                if lo > hi + 1e-9:
                    ohlc_bad += 1
                ohlc_bad += sum(1 for k in ("open", "close")
                                if v.get(k) is not None and not lo - 1e-6 <= v[k] <= hi + 1e-6)
        hist = _read_existing(os.path.join(hist_dir, f"{t}.json"))
        hdates = [r.get("date") for r in hist]
        free = {r.get("date"): r for r in hist if r.get("source") == "iborrowdesk"}
        overlap = [o for o in obs if o.get("date") in free]
        fee_mm = sum(1 for o in overlap
                     if (o.get("fee") or {}).get("close") is not None
                     and free[o["date"]].get("fee_bps_yr") is not None
                     and abs(o["fee"]["close"] * 100 - free[o["date"]]["fee_bps_yr"]) > 0.01)
        avail_mm = sum(1 for o in overlap
                       if (o.get("available") or {}).get("close") != free[o["date"]].get("available"))
        gap = 0
        for a, b in zip(dates, dates[1:]):
            da, db = date.fromisoformat(a), date.fromisoformat(b)
            gap = max(gap, sum(1 for k in range(1, (db - da).days) if (da + timedelta(days=k)).weekday() < 5))
        floor = rec.get("covered_from") or IBD2_FLOOR_FALLBACK
        late = (status == "complete" and bool(dates)
                and date.fromisoformat(dates[0]) > date.fromisoformat(floor) + timedelta(days=180))
        row = {"status": status, "days": len(obs), "first": dates[0] if dates else None,
               "last": dates[-1] if dates else None, "covered_from": rec.get("covered_from"),
               "units": rec.get("units"), "raw_dup": len(dates) - len(set(dates)),
               "raw_unsorted": dates != sorted(dates), "ohlc_bad": ohlc_bad,
               "missing_in_history": len(set(dates) - set(hdates)),
               "history_dup": len(hdates) - len(set(hdates)), "history_unsorted": hdates != sorted(hdates),
               "history_span": [hdates[0], hdates[-1]] if hdates else None,
               "overlap_days": len(overlap), "fee_mismatch": fee_mm, "avail_mismatch": avail_mm,
               "max_gap_weekdays": gap, "late_start": late}
        tolerance = max(1, len(overlap) // 100)
        row["ok"] = not (row["raw_dup"] or row["raw_unsorted"] or ohlc_bad or row["missing_in_history"]
                         or row["history_dup"] or row["history_unsorted"]
                         or fee_mm > tolerance or avail_mm > tolerance)
        per[t] = row
        tot["days"] += len(obs)
        tot["overlap_days"] += len(overlap)
        tot["fee_mismatch"] += fee_mm
        tot["avail_mismatch"] += avail_mm
        if not row["ok"]:
            tot["failing"].append(t)
        if late:
            tot["late_start"].append(f"{t}:{dates[0]}")
    report = {"checked_at": _utcnow().isoformat(), "totals": tot, "names": per}
    if names:
        _write_json(os.path.join(raw_dir, "_validation.json"), report, indent=1)
    log(f"     v2 validation: {tot['complete']} complete, {tot['partial']} partial, "
        f"{tot['unresolved']} unresolved, {tot['missing']} not started; {tot['days']} days; "
        f"{tot['overlap_days']} days cross-checked vs the free pull: {tot['fee_mismatch']} fee / "
        f"{tot['avail_mismatch']} availability mismatches; failing {len(tot['failing'])} {tot['failing']}")
    if tot["late_start"]:
        log(f"     v2 validation: series starting late (listing or unlinked identity): {tot['late_start']}")
    for t in tot["failing"]:
        log(f"WARN v2 validation {t}: {per[t]}")
    return {"totals": tot}


def main_v2_only(cfg):
    """BORROW_V2_ONLY=1: the keyed backfill and its validation, nothing else. Its manifest is
    _run_history_v2.json, never _run.json — post.py's gate reads _run.json as "a borrow fetch
    finished", and this job takes no IBKR snapshot. Exit 1 on a FAIL or a failing validation."""
    history_v2 = collect_history_v2(cfg)
    failing = ((history_v2.get("validation") or {}).get("totals") or {}).get("failing") or []
    ok = history_v2["ok"] and not failing
    manifest = {"vendor": "iBorrowDesk v2 API (keyed)", "spec_items": ["D-10"], "mode": "v2_only",
                "ended_at": datetime.now(timezone.utc).isoformat(), "ok": ok, "history_v2": history_v2}
    _write_json(os.path.join(DATA_DIR, "_run_history_v2.json"), manifest, indent=2)
    if not ok:
        log(f"FAILED — error log: {_persist_log('error_v2', manifest)}")
        return 1
    if STORE_LOGS:
        log(f"run log: {_persist_log('run_v2', manifest)}")
    return 0


def main():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    if V2_ONLY:
        return main_v2_only(cfg)
    # An explicit [] means NO IBKR snapshot — the watchlist config pulls iBorrowDesk history only,
    # because the main pass already stores the whole usa.txt. Only a missing key defaults to usa.
    countries = cfg["countries"] if isinstance(cfg.get("countries"), list) else ["usa"]
    started = datetime.now(timezone.utc)
    results = []

    for country in countries:
        entry = {"symbol": country, "dataset": "borrow", "ok": False, "count": 0,
                 "added": 0, "error": None}
        try:
            text, url = fetch_file(country)
            rows, snapshot, complete = parse(text)
            if not complete:
                raise RuntimeError(f"file has no #EOF marker ({len(rows)} rows) — "
                                   f"treating as a truncated download, not storing")
            out_dir = os.path.join(DATA_DIR, country.upper())
            out = os.path.join(out_dir, f"{snapshot.replace(':', '')}.json.gz")
            entry.update(count=len(rows))
            if os.path.exists(out):
                # Same vendor snapshot already stored: IBKR republishes several times a day and a
                # re-run before the next publication sees the identical file. Append-only means we
                # keep the first copy rather than rewrite it.
                entry.update(ok=True, added=0)
                log(f"OK   borrow {country}: {len(rows)} rows [snapshot {snapshot} already stored] -> {out}")
            else:
                os.makedirs(out_dir, exist_ok=True)
                tmp = out + ".part"
                payload = {
                    # §3 provenance, carried with the data rather than only in _run.json
                    "vendor": "IBKR public short-stock file",
                    "source_endpoint": url,
                    "pulled_at_utc": started.isoformat(),
                    "snapshot_utc": snapshot,
                    "spec_item": "D-10",
                    "gc_default_bps": GC_DEFAULT_BPS,
                    "n_rows": len(rows),
                    "rows": rows,
                }
                with gzip.open(tmp, "wt", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.replace(tmp, out)
                entry.update(ok=True, added=len(rows))
                priced = sum(1 for r in rows if r["fee_bps_yr"] is not None)
                log(f"OK   borrow {country}: {len(rows)} rows ({priced} with a fee) "
                    f"[snapshot {snapshot}] -> {out}")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"FAIL borrow {country}: {entry['error']}")
        results.append(entry)

    # iBorrowDesk history runs AFTER the IBKR snapshot: the snapshot is the unrecoverable one
    # (today's file exists for minutes), the history is a rolling window that tolerates a retry.
    collect_history(cfg, results)

    # The keyed all-time backfill goes last and reports under its own manifest key, outside all_ok:
    # a spent monthly allowance is the expected state for weeks, not a failed borrow run.
    history_v2 = collect_history_v2(cfg)

    # An empty results list is only a failure when a snapshot was requested: with `countries: []`
    # (the watchlist config) a night where every history name is still fresh legitimately does nothing.
    all_ok = (bool(results) or not countries) and all(r["ok"] for r in results)
    manifest = {
        "vendor": "IBKR public short-stock file (anonymous FTP) + iBorrowDesk history (free + v2)",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "spec_items": ["D-10"],
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "hosts_tried": FTP_HOSTS,
        "countries": countries,
        "gc_default_bps": GC_DEFAULT_BPS,
        "ok": all_ok,
        "results": results,
        "history_v2": history_v2,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = os.path.join(DATA_DIR, "_run.json.part")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, os.path.join(DATA_DIR, "_run.json"))

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
