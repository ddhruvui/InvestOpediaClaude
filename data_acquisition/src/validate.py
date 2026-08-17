#!/usr/bin/env python3
"""M1-04 cross-vendor validation + Q-004 data-quality checks + per-ticker gap repair.

Spec v1.2 §2 D-12 and §5. This is the job the spec specifies and that nothing implemented: pull
the vendors, compare them, and QUARANTINE what disagrees. Everything here reads the volume — no
vendor API calls, no credits, no tokens.

WHAT IT DOES

  1. D-12 CROSS-CHECK (M1-04, [IMPL-29]).  EODHD `close` vs Sharadar SEP `closeunadj` vs Tiingo
     `close`, per (ticker, session). A disagreement > `threshold_bps` (spec: 25) quarantines the
     ticker. Sharadar/Tiingo are the RAW-print reference: EODHD rewrites `close` retroactively after
     spinoffs, so its pre-event history is rescaled for affected names (measured: 4,524 breaches of
     621,729 comparisons, 4,500 of them in 5 names). Quarantine is emitted per ticker AND per date
     range so a consumer can drop just the tainted span instead of the whole symbol.

  2. Q-004 CHECKS (§5).
       missing bars        per-ticker sessions absent vs the D-11 XNYS calendar
       non-session rows    rows on dates the exchange was shut
       duplicate keys      repeated (ticker,date) without distinct `lastupdated`
       split sanity        a D-02 split with no matching raw-price jump, and vice versa
       adjusted continuity adjusted_close/close factor jumping without a split or dividend
       stale tickers       last bar far behind the calendar (a delisting, or a broken feed)

  3. TICKER-REUSE DETECTION.  Symbols get recycled: `Q` carried a different issuer before Qnity
     Electronics listed 2025-11-03, `SW` before Smurfit Westrock listed 2024-07-08. Rows dated
     before the entity master's `firstpricedate` belong to a DIFFERENT COMPANY and would otherwise
     be silently concatenated into one price series. Flagged per ticker with the cut date, and
     repaired when --repair is passed.

  4. REPAIR (--repair).  Two fixes that need no vendor call because the data is already on the
     volume in another form:
       * per-ticker `eod` holes filled from the corresponding eod_bulk day-file (EODHD's own two
         endpoints disagree: URI is missing 2023-04-06 in eod/URI while eod_bulk/2023-04-06 has it)
       * pre-`firstpricedate` rows from a recycled symbol dropped
     Repairs are written atomically and every one is listed in the report.

OUTPUT
    DATA_DIR/quarantine.json   consumer-facing: {ticker: {reason, from, to, ...}} — the M1-04 list
    DATA_DIR/report.json       every check, with counts and the offending keys
    DATA_DIR/logs/             gated by STORE_LOGS (errors always logged)

    DATA_DIR defaults to /workspace/data_quality; the vendor trees are read from
    EOD_DIR / SEP_DIR / TIINGO_DIR / BULK_DIR / SESSIONS_PATH / TICKERS_PATH (all defaulted to the
    standard volume layout, all overridable for local runs).

Exit code 0 when the job ran; a non-empty quarantine is a RESULT, not a failure. Exit 1 only if the
job could not run (missing calendar, unreadable inputs).

    DATA_DIR=./data_quality EOD_DIR=./data SEP_DIR=./data_nasdaq/SEP \
      SESSIONS_PATH=./data_calendar/XNYS.json TICKERS_PATH=./data_nasdaq/TICKERS/SHARADAR.json \
      python3 data_acquisition/src/validate.py --repair
"""
import glob
import json
import os
import sys
import traceback
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_quality")
EOD_DIR = os.environ.get("EOD_DIR", "/workspace/data")
SEP_DIR = os.environ.get("SEP_DIR", "/workspace/data_nasdaq/SEP")
TIINGO_DIR = os.environ.get("TIINGO_DIR", "/workspace/data_tiingo")
BULK_DIR = os.environ.get("BULK_DIR", "/workspace/data/eod_bulk/US")
SESSIONS_PATH = os.environ.get("SESSIONS_PATH", "/workspace/data_calendar/XNYS.json")
TICKERS_PATH = os.environ.get("TICKERS_PATH", "/workspace/data_nasdaq/TICKERS/SHARADAR.json")
SPLITS_DIR = os.environ.get("SPLITS_DIR", "/workspace/data/splits")
SF1_DIR = os.environ.get("SF1_DIR", "/workspace/data_nasdaq/SF1")
ACTIONS_DIR = os.environ.get("ACTIONS_DIR", "/workspace/data_nasdaq/ACTIONS")
DIV_DIR = os.environ.get("DIV_DIR", "/workspace/data/dividends")

THRESHOLD_BPS = float(os.environ.get("XCHECK_BPS", "25"))       # spec D-12
STALE_SESSIONS = int(os.environ.get("STALE_SESSIONS", "5"))
# Pre-listing rows within this many sessions of the listing date, and this few, are read as
# when-issued prints rather than a recycled symbol (see the ticker-reuse check).
WI_MAX_GAP_SESSIONS = int(os.environ.get("WI_MAX_GAP_SESSIONS", "15"))
WI_MAX_ROWS = int(os.environ.get("WI_MAX_ROWS", "30"))
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")
REPAIR = "--repair" in sys.argv
# Deleting rows on an identity heuristic is opt-in; see the repair section.
DROP_REUSE = "--drop-reuse" in sys.argv

_LOG = []


def log(m):
    print(m, flush=True)
    _LOG.append(m)


def _load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _persist_log(kind, payload=None):
    d = os.path.join(DATA_DIR, "logs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log")
    with open(p, "w") as f:
        f.write("\n".join(_LOG) + "\n")
        if payload is not None:
            f.write("\n--- report ---\n" + json.dumps(payload, indent=2) + "\n")
    return p


def _ticker_of(path):
    return os.path.basename(path)[:-5]


def _by_date_latest(rows, date_key="date", vintage_key="lastupdated"):
    """Collapse rows to one per date, keeping the newest vintage (M1-01 stores every restatement)."""
    best = {}
    for r in rows:
        d = str(r.get(date_key) or "")[:10]
        if not d:
            continue
        cur = best.get(d)
        if cur is None or str(r.get(vintage_key) or "") >= str(cur.get(vintage_key) or ""):
            best[d] = r
    return best


def load_universe():
    eod = {}
    for p in sorted(glob.glob(os.path.join(EOD_DIR, "*.json"))):
        t = _ticker_of(p)
        if t.startswith("_"):
            continue
        rows = _load(p)
        if isinstance(rows, list):
            eod[t] = rows
    return eod


def main():
    started = datetime.now(timezone.utc)
    cal = _load(SESSIONS_PATH)
    if not cal or not cal.get("sessions"):
        print(f"FATAL: no D-11 session calendar at {SESSIONS_PATH} — run fetch_calendar.py first",
              file=sys.stderr)
        return 1
    sessions = cal["sessions"]
    S = set(sessions)
    idx = {d: i for i, d in enumerate(sessions)}

    eod = load_universe()
    if not eod:
        print(f"FATAL: no price files under {EOD_DIR}", file=sys.stderr)
        return 1
    log(f"     loaded {len(eod)} tickers from {EOD_DIR}; calendar {cal.get('calendar')} "
        f"{cal['n_sessions']} sessions")

    # entity master -> first legitimate price date per symbol (ticker-reuse detection)
    first_price = {}
    for r in (_load(TICKERS_PATH) or []):
        t, fp = r.get("ticker"), r.get("firstpricedate")
        if t and fp and (t not in first_price or fp < first_price[t]):
            first_price[t] = str(fp)[:10]

    quarantine = {}
    report = {
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "spec_items": ["D-12", "M1-04", "Q-004", "§5"],
        "generated_at_utc": started.isoformat(),
        "threshold_bps": THRESHOLD_BPS,
        "calendar": {"name": cal.get("calendar"), "package_version": cal.get("package_version")},
        "n_tickers": len(eod),
        "repair_mode": REPAIR,
        "checks": {},
        "repairs": [],
    }

    def add_q(ticker, reason, **kw):
        quarantine.setdefault(ticker, []).append(dict(reason=reason, **kw))

    # ---------------------------------------------------------------- 1) ticker reuse vs when-issued
    # Rows before the entity master's firstpricedate are one of TWO different things:
    #   * WHEN-ISSUED trading — a handful of sessions immediately before the regular-way listing.
    #     EODHD carries them, Sharadar does not. Real trades in the SAME security; keep them.
    #   * A RECYCLED SYMBOL — a block of rows belonging to a DIFFERENT issuer, separated from the
    #     listing by a long dead period. Concatenating those into one price series invents returns.
    # The discriminator is the GAP between the last pre-listing row and the listing date, not the
    # mere existence of early rows. Measured: GEV/CEG/VLTO/SOLV have 3-10 contiguous WI sessions;
    # TKO has 534 rows ending long before its 2023-09-12 listing.
    reuse, when_issued = {}, {}
    for t, rows in eod.items():
        fp = first_price.get(t)
        if not fp or fp not in idx:
            continue
        bad = sorted({r["date"] for r in rows if r.get("date") and r["date"] < fp})
        if not bad:
            continue
        gap = idx[fp] - idx[bad[-1]] if bad[-1] in idx else 10 ** 6
        info = {"first_price_date": fp, "n_rows_before": len(bad),
                "from": bad[0], "to": bad[-1], "gap_sessions_to_listing": gap}
        # THE GAP IS THE ONLY EVIDENCE. An earlier version also required len(bad) <= 30, which
        # misread every re-domicile, merger and rename as a recycled symbol: STE (STERIS re-domiciled
        # 2015), LIN, BKR, EVRG, GOOG and KKR all have a series running continuously to the day
        # before `firstpricedate` — gap of 1 session — and it deleted 30,671 rows of their own
        # history. Row count says nothing about identity. A genuinely recycled symbol has a DEAD
        # PERIOD: Q traded once in 2024 then nothing for 204 sessions before Qnity listed.
        if gap <= WI_MAX_GAP_SESSIONS:
            when_issued[t] = info      # continuous into the listing: when-issued, or a corporate
                                       # event that kept the tape running. Keep it.
        else:
            reuse[t] = info
            add_q(t, "ticker_reuse", detail=f"rows before firstpricedate {fp} are a prior issuer "
                                            f"({gap} sessions of dead time before the listing)",
                  **{"from": bad[0], "to": bad[-1], "n_rows": len(bad)})
    report["checks"]["ticker_reuse"] = {"n_tickers": len(reuse), "detail": reuse}
    report["checks"]["when_issued_prints"] = {"n_tickers": len(when_issued), "detail": when_issued,
                                              "note": "benign — real trades in the same security "
                                                      "before regular-way listing; NOT quarantined"}
    log(f"OK   ticker_reuse      : {len(reuse)} recycled -> {sorted(reuse)}")
    log(f"OK   when_issued       : {len(when_issued)} ticker(s) with pre-listing WI prints (kept) "
        f"-> {sorted(when_issued)}")

    # ---------------------------------------------------------------- 2) Q-004 structural checks
    non_session, dup_keys, missing_bars, stale = {}, {}, {}, {}
    # The calendar deliberately runs YEARS into the future (D-11 supplies forward sessions for the
    # t+h+1 MOO). sessions[-1] is therefore 2029-12-31, not today — comparing a last bar against it
    # marks every healthy ticker as stale. Anchor on the most recent session that has happened.
    today = datetime.now(timezone.utc).date().isoformat()
    past = [d for d in sessions if d <= today]
    last_session = past[-1] if past else sessions[-1]
    for t, rows in eod.items():
        ds = [r["date"] for r in rows if r.get("date")]
        bad = sorted({d for d in ds if d not in S})
        if bad:
            non_session[t] = bad
            add_q(t, "non_session_rows", n_rows=len(bad), sample=bad[:5])
        seen, dups = set(), set()
        for d in ds:
            (dups if d in seen else seen).add(d)
        if dups:
            dup_keys[t] = sorted(dups)
            add_q(t, "duplicate_date_rows", n_rows=len(dups), sample=sorted(dups)[:5])
        if ds:
            lo = max(min(ds), first_price.get(t, min(ds)))
            hi = max(ds)
            gaps = [d for d in sessions if lo <= d <= hi and d not in set(ds)]
            if gaps:
                missing_bars[t] = gaps
            # a feed that stopped: last bar well behind the calendar
            if hi in idx and idx[last_session] - idx[hi] > STALE_SESSIONS:
                stale[t] = {"last_bar": hi, "sessions_behind": idx[last_session] - idx[hi]}
    for name, d in (("non_session_rows", non_session), ("duplicate_date_rows", dup_keys),
                    ("missing_bars", missing_bars), ("stale_feed", stale)):
        report["checks"][name] = {"n_tickers": len(d),
                                  "n_rows": sum(len(v) if isinstance(v, list) else 1 for v in d.values()),
                                  "detail": {k: v for k, v in list(d.items())[:40]}}
    log(f"OK   non_session_rows  : {len(non_session)} ticker(s)")
    log(f"OK   duplicate_dates   : {len(dup_keys)} ticker(s)")
    log(f"OK   missing_bars      : {len(missing_bars)} ticker(s), "
        f"{sum(len(v) for v in missing_bars.values())} session(s) total")
    log(f"OK   stale_feed        : {len(stale)} ticker(s) > {STALE_SESSIONS} sessions behind "
        f"(delistings show up here) -> {sorted(stale)[:6]}")

    # ---------------------------------------------------------------- 2b) carried-forward bars
    # EODHD occasionally emits a placeholder instead of a quote: volume 0, open==high==low==close,
    # and close identical to the previous session — the prior day carried forward. Measured on this
    # volume: 28 such bars across 12 tickers, clustered on 2026-07-21/22 and 2026-07-31, with
    # Sharadar and Tiingo agreeing on a genuinely different close for 27 of them (worst TPL 405.87
    # vs 433.10, 629 bps). Only 22 breach the D-12 25 bps threshold, so the cross-check alone misses
    # a quarter of them — and a zero-volume bar corrupts F6 turnover regardless of the close.
    carried = {}
    for t, rows in eod.items():
        by = sorted((r for r in rows if r.get("date")), key=lambda r: r["date"])
        for prev, cur in zip(by, by[1:]):
            o, h, lo, c, v = (cur.get(k) for k in ("open", "high", "low", "close", "volume"))
            if v == 0 and c and o == h == lo == c and prev.get("close") == c:
                carried.setdefault(t, []).append(cur["date"])
    if carried:
        for t, ds in carried.items():
            add_q(t, "carried_forward_bar", n_rows=len(ds), sample=ds[:5],
                  detail="volume 0, o=h=l=c, close equal to the prior session — a placeholder, not a quote")
    report["checks"]["carried_forward_bars"] = {
        "n_tickers": len(carried), "n_rows": sum(len(v) for v in carried.values()), "detail": carried}
    log(f"OK   carried_forward   : {sum(len(v) for v in carried.values())} placeholder bar(s) across "
        f"{len(carried)} ticker(s) -> {sorted(carried)[:8]}")

    # ---------------------------------------------------------------- 3) D-12 cross-vendor close
    def load_ref(dirpath, close_key, date_key="date", vintage="lastupdated", strip_time=True):
        out = {}
        for p in glob.glob(os.path.join(dirpath, "*.json")):
            t = _ticker_of(p)
            if t.startswith("_"):
                continue
            rows = _load(p)
            if not isinstance(rows, list) or not rows:
                continue
            best = _by_date_latest(rows, date_key, vintage)
            out[t] = {d: r.get(close_key) for d, r in best.items() if r.get(close_key) is not None}
        return out

    sep = load_ref(SEP_DIR, "closeunadj")
    tii = load_ref(TIINGO_DIR, "close")
    # full SEP rows (close AND closeunadj) + SF1, for the PIT share-basis section below
    sep_full = {}
    for p in glob.glob(os.path.join(SEP_DIR, "*.json")):
        t = _ticker_of(p)
        if t.startswith("_"):
            continue
        rows = _load(p)
        if isinstance(rows, list) and rows:
            sep_full[t] = _by_date_latest(rows)
    sf1_by_ticker = {}
    for p in glob.glob(os.path.join(SF1_DIR, "*.json")):
        t = _ticker_of(p)
        if t.startswith("_"):
            continue
        rows = _load(p)
        if isinstance(rows, list) and rows:
            # newest vintage per (calendardate) — PIT consumers filter by lastupdated themselves
            sf1_by_ticker[t] = list(_by_date_latest(rows, "calendardate").values())
    log(f"     reference vendors: Sharadar SEP {len(sep)} tickers, Tiingo {len(tii)} tickers")

    breaches, compared = {}, 0
    for t, rows in eod.items():
        refs = [(n, m[t]) for n, m in (("sharadar", sep), ("tiingo", tii)) if t in m]
        if not refs:
            continue
        hits = []
        for r in rows:
            d, c = r.get("date"), r.get("close")
            if not d or not c:
                continue
            for name, m in refs:
                v = m.get(d)
                if v is None:
                    continue
                compared += 1
                bps = abs(v - c) / c * 1e4
                if bps > THRESHOLD_BPS:
                    hits.append((d, name, c, v, round(bps, 1)))
        if hits:
            ds = sorted(h[0] for h in hits)
            worst = max(hits, key=lambda h: h[4])
            breaches[t] = {"n": len(hits), "from": ds[0], "to": ds[-1],
                           "worst_bps": worst[4], "worst": {"date": worst[0], "vendor": worst[1],
                                                            "eodhd_close": worst[2], "ref_close": worst[3]}}
            # Contiguous block ending at a corporate action => EODHD rescaled its whole pre-event
            # history. Isolated days are ordinary vendor glitches; both quarantine, with the span
            # recorded so a consumer can drop just the tainted range.
            # `dates` is the load-bearing field, not `from`/`to`. Recording only the endpoints
            # made the span the whole history for most tickers — CHD breached on 21 days and the
            # range 2000-01-05..2026-07-31 covers 9,704 of them — so build_m1 masked 1,133,450 rows
            # for 169,146 real disagreements, 38% of the table. Keep every breaching date so a
            # consumer drops exactly the bad bars. from/to stay for reporting and back-compat.
            add_q(t, "close_disagreement", **{"from": ds[0], "to": ds[-1], "n_rows": len(hits),
                                              "dates": ds, "worst_bps": worst[4],
                                              "systematic": len(hits) > 50})
    report["checks"]["close_cross_check"] = {
        "compared": compared, "n_tickers": len(breaches),
        "n_breaches": sum(v["n"] for v in breaches.values()),
        "systematic_tickers": sorted(t for t, v in breaches.items() if v["n"] > 50),
        "detail": breaches}
    log(f"OK   close_cross_check : {sum(v['n'] for v in breaches.values()):,} breach(es) > "
        f"{THRESHOLD_BPS:g} bps over {compared:,} comparisons, {len(breaches)} ticker(s); "
        f"systematic: {sorted(t for t, v in breaches.items() if v['n'] > 50)}")

    # ---------------------------------------------------------------- 4) split sanity (§5)
    # "split sanity <- D-01 raw jump vs D-02 ratio". Two directions, and BOTH need the ratio, not
    # just the size of the move: a 20%-move trigger fires on ordinary earnings gaps (SMCI has 16 in
    # five years) and says nothing about splits.
    #   a) every split on file should move the raw close by ~its ratio
    #   b) an unexplained move only counts if the ratio is close to a plausible split fraction
    # Candidate ratios deliberately EXCLUDE 1.5 / 2:3 and anything inside [0.55, 1.9]. A 3:2 split
    # and a big earnings gap are indistinguishable from the price series alone: NFLX -35% on the
    # 2022 subscriber miss, SMCI -33% on the auditor resignation and APP +46% on earnings all land
    # within 3% of 2/3 or 1.5. Flagging those produced nine false positives and zero real ones.
    # Only ratios a market move essentially never reaches are usable evidence of a missing split.
    COMMON = [2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 50]
    RATIOS = sorted(r for r in set([float(x) for x in COMMON] + [1.0 / x for x in COMMON])
                    if r >= 1.9 or r <= 0.55)

    def near_split_ratio(x, tol=0.02):
        return next((r for r in RATIOS if abs(x / r - 1) <= tol), None)

    split_issues = {}
    for t, rows in eod.items():
        sdates = {}
        for s in (_load(os.path.join(SPLITS_DIR, f"{t}.json")) or []):
            try:
                a, b = str(s.get("split", "")).split("/")
                sdates[str(s.get("date"))[:10]] = float(a) / float(b)
            except (ValueError, ZeroDivisionError, AttributeError):
                continue
        by = {r["date"]: r for r in rows if r.get("date") and r.get("close")}
        ds = sorted(by)
        problems = []
        for i in range(1, len(ds)):
            d, p0, p1 = ds[i], by[ds[i - 1]]["close"], by[ds[i]]["close"]
            if not p0 or not p1:
                continue
            ratio = p1 / p0
            if d in sdates:
                # (a) declared split: raw close should move by ~1/ratio (EODHD "A/B" => A-for-B)
                expect = 1.0 / sdates[d] if sdates[d] else None
                if expect and abs(ratio / expect - 1) > 0.25 and abs(expect - 1) > 0.2:
                    problems.append({"date": d, "kind": "declared split, price did not move by it",
                                     "split_ratio": sdates[d], "price_ratio": round(ratio, 4)})
            else:
                # (b) undeclared move that looks exactly like a split
                hit = near_split_ratio(ratio)
                if hit is not None and abs(hit - 1) > 0.2:
                    problems.append({"date": d, "kind": "price moved by a split-like ratio with no "
                                                        "split on file",
                                     "price_ratio": round(ratio, 4), "nearest_split_ratio": hit})
        if problems:
            split_issues[t] = problems[:10]
            for p in problems[:1]:
                add_q(t, "split_sanity", detail=p["kind"], date=p["date"])
    report["checks"]["split_sanity"] = {"n_tickers": len(split_issues), "detail": split_issues}
    log(f"OK   split_sanity      : {len(split_issues)} ticker(s) -> {sorted(split_issues)[:8]}")

    # ---------------------------------------------------------------- 4b) PIT shares basis (mcap)
    # Spec §2 de-scopes EODHD's historical market cap in favour of `mcap := raw_close x shares_PIT`.
    # Taken literally that is WRONG, and silently so. Sharadar's SF1 share counts are stated on the
    # CURRENT split-adjusted basis, the same basis as SEP `close` — never `closeunadj`:
    #   SF1 price x sharesbas == SF1 marketcap on 10,784 rows
    #   SF1 price == SEP close on 10,781 rows, == SEP closeunadj on only 10,298
    # So pairing the RAW close with SF1 shares overstates mcap by every split since the filing.
    # NVDA 2021-09-30: closeunadj 319.56 x 25.02e9 = $7,995B, against a true $799.5B (the 2024-06-10
    # 10:1). 45 of 503 tickers split inside the window, and F7 mcap plus any size/value sort and the
    # top1000_dollar_volume universe screen all inherit the error.
    #
    # Rather than only warn, emit the share count on BOTH bases so either pairing is correct:
    #   shares_current_basis  x  SEP close        -> mcap   (current units)
    #   shares_asof_basis     x  SEP closeunadj   -> mcap   (as-of-filing units)
    # shares_asof = shares_current / (product of split ratios strictly AFTER the filing date).
    def split_timeline(t):
        out = []
        for r in (_load(os.path.join(ACTIONS_DIR, f"{t}.json")) or []):
            if r.get("action") == "split" and r.get("value"):
                try:
                    out.append((str(r["date"])[:10], float(r["value"])))
                except (TypeError, ValueError):
                    continue
        return sorted(out)

    shares_rows, basis_bad, checked = [], {}, 0
    for t, rows in sf1_by_ticker.items():
        splits = split_timeline(t)
        sepm = sep_full.get(t, {})
        for r in rows:
            dk = str(r.get("date") or "")[:10]
            sb = r.get("sharesbas")
            if not dk or sb in (None, ""):
                continue
            try:
                sb = float(sb)
            except (TypeError, ValueError):
                continue
            f_after = 1.0
            for d, v in splits:
                if d > dk and v:
                    f_after *= v
            row = {"ticker": t, "calendardate": r.get("calendardate"), "filing_date": dk,
                   "lastupdated": r.get("lastupdated"),
                   "shares_current_basis": sb, "shares_asof_basis": sb / f_after,
                   "split_factor_after_filing": f_after,
                   "shareswa_current_basis": r.get("shareswa"),
                   "sf1_price": r.get("price"), "sf1_marketcap": r.get("marketcap")}
            shares_rows.append(row)
            # assertion: as-of shares x RAW close must reproduce SF1's own marketcap
            s = sepm.get(dk)
            mc = r.get("marketcap")
            if s and s.get("closeunadj") and mc:
                try:
                    err = abs(float(s["closeunadj"]) * row["shares_asof_basis"] / float(mc) - 1)
                    checked += 1
                    if err > 0.02:
                        basis_bad.setdefault(t, []).append({"filing_date": dk, "rel_err": round(err, 4)})
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
    _write(os.path.join(DATA_DIR, "shares_pit.json"), shares_rows)
    report["checks"]["shares_pit_basis"] = {
        "rows": len(shares_rows), "assertion_checked": checked,
        "n_tickers_failing_2pct": len(basis_bad),
        "tickers_with_in_window_split": sorted({t for t in sf1_by_ticker if split_timeline(t)}),
        "detail": dict(list(basis_bad.items())[:20]),
        "rule": "mcap = shares_current_basis x SEP close  OR  shares_asof_basis x SEP closeunadj. "
                "NEVER closeunadj x sharesbas — that is the spec's literal wording and it overstates "
                "mcap by every split after the filing."}
    log(f"OK   shares_pit_basis  : {len(shares_rows):,} rows -> shares_pit.json "
        f"({len({t for t in sf1_by_ticker if split_timeline(t)})} tickers split in-window); "
        f"as-of-basis assertion failed for {len(basis_bad)} ticker(s) of {checked:,} checked")

    # ---------------------------------------------------------------- 5) repair
    if REPAIR:
        # (a) fill per-ticker holes from eod_bulk — EODHD's own two endpoints disagree
        want = {}
        for t, gaps in missing_bars.items():
            for d in gaps:
                want.setdefault(d, []).append(t)
        # placeholder bars are replaced from the same source, not just holes
        for t, ds in carried.items():
            for d in ds:
                want.setdefault(d, []).append(t)
        filled = 0
        for d in sorted(want):
            bulk = _load(os.path.join(BULK_DIR, f"{d}.json"))
            if not isinstance(bulk, list):
                continue
            m = {r.get("code"): r for r in bulk}
            for t in want[d]:
                src = m.get(t)
                if not src or src.get("close") is None:
                    continue
                row = {k: src.get(k) for k in
                       ("date", "open", "high", "low", "close", "adjusted_close", "volume")}
                if row.get("volume") == 0 and row.get("open") == row.get("close"):
                    continue          # the bulk file carries the same placeholder — nothing to gain
                was = [r for r in eod[t] if r.get("date") == d]
                eod[t] = [r for r in eod[t] if r.get("date") != d]
                eod[t].append(row)
                report["repairs"].append({"ticker": t, "date": d,
                                          "fix": ("placeholder bar replaced from eod_bulk" if was
                                                  else "gap filled from eod_bulk")})
                filled += 1
        # (b) rows from a previous issuer of a recycled symbol.
        # OFF BY DEFAULT. Deleting price history on a heuristic is not a repair — the first version
        # of this removed 30,671 legitimate rows before the gap rule was corrected. The quarantine
        # entry is the durable signal; pass --drop-reuse only when you have checked the names.
        dropped = 0
        for t, info in (reuse.items() if DROP_REUSE else ()):
            fp = info["first_price_date"]
            before = len(eod[t])
            eod[t] = [r for r in eod[t] if r.get("date", "") >= fp]
            n = before - len(eod[t])
            if n:
                report["repairs"].append({"ticker": t, "fix": f"dropped {n} rows before "
                                                              f"firstpricedate {fp} (prior issuer)"})
                dropped += n
        touched = {r["ticker"] for r in report["repairs"]}
        for t in touched:
            rows = sorted(eod[t], key=lambda r: r.get("date") or "")
            _write(os.path.join(EOD_DIR, f"{t}.json"), rows)
        log(f"OK   repair            : filled {filled} missing bar(s) from eod_bulk, dropped "
            f"{dropped} recycled-symbol row(s), rewrote {len(touched)} file(s)")
    else:
        log("     repair            : SKIPPED (pass --repair to apply)")

    # ---------------------------------------------------------------- emit
    q_out = {t: {"quarantined_at_utc": started.isoformat(), "issues": v} for t, v in quarantine.items()}
    _write(os.path.join(DATA_DIR, "quarantine.json"), q_out, indent=2)
    report["quarantined_tickers"] = sorted(quarantine)
    report["n_quarantined"] = len(quarantine)
    _write(os.path.join(DATA_DIR, "report.json"), report, indent=2)
    log(f"OK   QUARANTINE        : {len(quarantine)} ticker(s) -> {os.path.join(DATA_DIR, 'quarantine.json')}")
    log(f"     report            : {os.path.join(DATA_DIR, 'report.json')}")
    if STORE_LOGS:
        _persist_log("run", report)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            _LOG.append(traceback.format_exc())
            _persist_log("crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
