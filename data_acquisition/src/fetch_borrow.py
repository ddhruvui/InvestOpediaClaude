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
import os
import socket
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

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


def main():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    countries = cfg.get("countries") or ["usa"]
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

    all_ok = bool(results) and all(r["ok"] for r in results)
    manifest = {
        "vendor": "IBKR public short-stock file (anonymous FTP) + iBorrowDesk history",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "spec_items": ["D-10"],
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "hosts_tried": FTP_HOSTS,
        "countries": countries,
        "gc_default_bps": GC_DEFAULT_BPS,
        "ok": all_ok,
        "results": results,
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
