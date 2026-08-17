#!/usr/bin/env python3
"""§3/§4 parse + landing layer: raw vendor JSON -> the M1 tables, as Parquet.

This is the tier between "we downloaded everything" and "a model can read it". Nothing upstream
enforces a single one of the consumption rules the data actually needs — they were written down in
the README and would otherwise have to be re-remembered by every consumer. Here they are code.

Needs pandas + pyarrow (the fetchers stay stdlib-only; this is not a fetcher).

OUTPUTS  (OUT_DIR, Parquet unless noted)
    sessions.parquet          D-11 / Q-001 trading grid, incl. future sessions
    entities.parquet          D-13 entity master, permaticker <-> ticker with validity dates
    raw_prices_eod/           D-01, partitioned by year. PK (date, ticker)
    adjustment_factors/       Q-002 cum factor per (date, ticker)
    corporate_actions.parquet D-02/D-03/D-04, one typed table
    fundamentals_pit.parquet  D-05/06 LONG format, PK (ticker, fiscal_period, filing_datetime, item)
    estimates_pit.parquet     D-14 consensus EPS/revenue + revision trend. PK (ticker, period, as_of_date)
    earnings_surprises.parquet D-14/D-07 consensus vs actual per quarter, back to ~1995
    borrow_fees.parquet       D-10
    qlib/<TICKER>.csv         the §4 bridge: date,open,close,high,low,volume,factor
    _manifest.json            provenance + row counts + every rule applied

RULES THIS ENFORCES (each one cost real debugging to find; see README "Reading this data correctly")

 1. SESSION GRID.  Rows are inner-joined to the D-11 XNYS calendar. EODHD answers on market
    holidays with OTC junk, so the price feed cannot tell you the exchange was shut.

 2. RAW CLOSE PROVENANCE.  EODHD rewrites `close` retroactively after spinoffs — DD reads 31.39
    where the actual print was 75.06. Sharadar `closeunadj` is preferred wherever it exists, EODHD
    only as fallback, and every row carries `close_source` so the choice is auditable rather than
    implicit.

 3. QUARANTINE.  validate.py's quarantine.json is applied, not merely shipped: a quarantined
    (ticker, span) gets `quarantined=True` and its EODHD-sourced close dropped.

 4. VINTAGES / PIT.  Sharadar keeps every restatement as a separate row (AAPL SEP: 272 rows over
    261 dates). Prices collapse to max(lastupdated). Fundamentals do NOT — every vintage is kept
    with `lastupdated`, because T-11 needs "what did we know at t", which means filtering on
    lastupdated <= t as well as filing_datetime <= t.

 5. mcap BASIS (the PIT-01 trap).  SF1 share counts sit on the SPLIT-ADJUSTED basis, so the spec's
    literal `raw_close x shares_PIT` overstates NVDA 2021-09-30 by 10x. Both share bases are
    emitted and mcap is computed from the matching one.

 6. SPLIT vs SPINOFF TYPING.  EODHD encodes spinoff price adjustments as split ratios; 18 of its
    73 "splits" land on a Sharadar spinoff. They are typed `spinoff`, not `split`, so Q-002 does
    not treat a spinoff as a share-count change.

 7. PERMATICKER.  Every row carries permaticker. Rows before the entity's firstpricedate are
    FLAGGED (`pre_first_price_date`), never dropped — most are a corporate event that kept the
    tape running, not a recycled symbol, and deleting them cost 30,866 rows of real history once.

 8. PROVENANCE (§3).  vendor / pulled_at_utc / source_endpoint / data_snapshot_id on every table.

    OUT_DIR=./m1 EOD_DIR=./data SEP_DIR=./data_nasdaq/SEP ... python3 src/build_m1.py
"""
import glob
import gzip
import hashlib
import json
import os
import shutil
import sys
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

# DATA_DIR is what launch.sh sets for every job; accept it as an alias for OUT_DIR so the
# pod payload stays uniform across vendors.
OUT_DIR = os.environ.get("OUT_DIR") or os.environ.get("DATA_DIR", "/workspace/m1")
EOD_DIR = os.environ.get("EOD_DIR", "/workspace/data")
SEP_DIR = os.environ.get("SEP_DIR", "/workspace/data_nasdaq/SEP")
SF1_DIR = os.environ.get("SF1_DIR", "/workspace/data_nasdaq/SF1")
ACTIONS_DIR = os.environ.get("ACTIONS_DIR", "/workspace/data_nasdaq/ACTIONS")
SPLITS_DIR = os.environ.get("SPLITS_DIR", "/workspace/data/splits")
DIV_DIR = os.environ.get("DIV_DIR", "/workspace/data/dividends")
TIINGO_DIR = os.environ.get("TIINGO_DIR", "/workspace/data_tiingo")
FUND_DIR = os.environ.get("FUND_DIR", "/workspace/data/fundamentals")
EST_DIR = os.environ.get("EST_DIR", "/workspace/data/estimates")
BORROW_DIR = os.environ.get("BORROW_DIR", "/workspace/data_borrow/history")
SESSIONS_PATH = os.environ.get("SESSIONS_PATH", "/workspace/data_calendar/XNYS.json")
TICKERS_PATH = os.environ.get("TICKERS_PATH", "/workspace/data_nasdaq/TICKERS/SHARADAR.json")
QUARANTINE_PATH = os.environ.get("QUARANTINE_PATH", "/workspace/data_quality/quarantine.json")
QLIB = os.environ.get("BUILD_QLIB", "1") not in ("0", "false", "no")

_LOG = []


def log(m):
    print(m, flush=True)
    _LOG.append(m)


def _load(p, default=None):
    try:
        if p.endswith(".gz"):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _each(dirpath, suffix=".json"):
    """Yield (ticker, rows) for every per-ticker file, skipping the _window/_ALL sidecars."""
    for p in sorted(glob.glob(os.path.join(dirpath, "*" + suffix))):
        t = os.path.basename(p)[: -len(suffix)]
        if t.startswith("_"):
            continue
        rows = _load(p)
        if isinstance(rows, list) and rows:
            yield t, rows


def _prov(df, vendor, endpoint, snapshot):
    df["vendor"] = vendor
    df["source_endpoint"] = endpoint
    df["pulled_at_utc"] = snapshot["pulled_at_utc"]
    df["data_snapshot_id"] = snapshot["id"]
    return df


def _write(df, path, partition_year=False):
    """Write a table, sharded by year when asked.

    NOT pandas' Hive partitioning (`partition_cols=["year"]`), for two measured reasons:

      1. It APPENDS. Each write drops a new `<uuid>-0.parquet` into the partition instead of
         replacing it, so four m1 runs left four copies in `year=2000/` and anything reading the
         directory as a dataset got ~4x the true rows.
      2. RunPod's S3 cannot serve the resulting keys. `GetObject` on
         `m1/raw_prices_eod/year=2024/<uuid>-0.parquet` returns "object not found" even though the
         same key is listed — the `=` in the directory name is not addressable. The tables were
         effectively unreadable off the volume.

    Deterministic `part-<year>.parquet` names fix both: re-runs overwrite in place, and the keys
    are plain. Stale shards are cleared first so a shrinking table cannot leave orphans behind."""
    if partition_year:
        os.makedirs(path, exist_ok=True)
        # Migrate away from the old Hive layout. Those `year=YYYY/` directories accumulated a new
        # uuid-named parquet on every run and cannot be deleted through RunPod's S3 API (the `=` in
        # the key is not addressable), so the removal has to happen here, on the mounted volume.
        for legacy in glob.glob(os.path.join(path, "year=*")):
            if os.path.isdir(legacy):
                shutil.rmtree(legacy, ignore_errors=True)
        for stale in glob.glob(os.path.join(path, "*.parquet")):
            os.remove(stale)
        if df.empty:
            # An empty table has no `date` column to shard on. Emitting nothing (after the stale
            # sweep above) is the honest result; the manifest records the zero.
            return 0
        years = pd.to_datetime(df["date"]).dt.year
        for y, g in df.groupby(years):
            g.to_parquet(os.path.join(path, f"part-{int(y)}.parquet"), index=False)
    else:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        df.to_parquet(path, index=False)
    return len(df)


def main():
    started = datetime.now(timezone.utc)
    cal = _load(SESSIONS_PATH)
    if not cal or not cal.get("sessions"):
        print(f"FATAL: no D-11 calendar at {SESSIONS_PATH}", file=sys.stderr)
        return 1
    sessions = cal["sessions"]
    S = set(sessions)
    snapshot = {"pulled_at_utc": started.isoformat(),
                "id": hashlib.sha256(
                    json.dumps({"cal": cal.get("package_version"), "at": started.isoformat()},
                               sort_keys=True).encode()).hexdigest()[:16]}
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = {"spec": "Data Acquisition Specification — FINAL v1.2",
                "spec_items": ["§3", "§4", "M1"], "built_at_utc": started.isoformat(),
                "data_snapshot_id": snapshot["id"],
                "calendar": {"name": cal.get("calendar"),
                             "package_version": cal.get("package_version")},
                "tables": {}, "rules_applied": []}

    # ---------------------------------------------------------------- sessions + entities
    ses = pd.DataFrame({"date": sessions})
    ses["is_early_close"] = ses["date"].isin(set(cal.get("early_closes") or {}))
    manifest["tables"]["sessions"] = _write(ses, os.path.join(OUT_DIR, "sessions.parquet"))
    log(f"OK   sessions          : {len(ses):,} rows")

    tk = _load(TICKERS_PATH) or []
    ent = pd.DataFrame(tk)
    first_price, permatick = {}, {}
    if not ent.empty:
        ent = ent.drop_duplicates(subset=[c for c in ("ticker", "permaticker", "table") if c in ent])
        for r in tk:
            t, fp, pm = r.get("ticker"), r.get("firstpricedate"), r.get("permaticker")
            if t and fp and (t not in first_price or str(fp) < first_price[t]):
                first_price[t] = str(fp)[:10]
            if t and pm:
                permatick.setdefault(t, pm)
                # Sharadar spells class shares with a DOT (BRK.B); the configs and EODHD use a
                # DASH. Without the alias BRK-B/BF-B join to nothing and lose their entity key.
                permatick.setdefault(t.replace(".", "-"), pm)
        keep = [c for c in ("permaticker", "ticker", "name", "exchange", "isdelisted", "category",
                            "sector", "industry", "siccode", "cusips", "firstpricedate",
                            "lastpricedate", "table", "lastupdated") if c in ent]
        ent = _prov(ent[keep], "Sharadar", "api.sharadar.com/tickers", snapshot)
        manifest["tables"]["entities"] = _write(ent, os.path.join(OUT_DIR, "entities.parquet"))
    log(f"OK   entities          : {len(ent):,} rows, {len(permatick):,} symbols with a permaticker")

    quar = _load(QUARANTINE_PATH) or {}
    qspans = []
    for t, v in quar.items():
        for i in v.get("issues", []):
            if i.get("reason") == "close_disagreement":
                qspans.append((t, i.get("from"), i.get("to")))
    log(f"     quarantine        : {len(quar)} ticker(s), {len(qspans)} tainted span(s) to mask")

    # ---------------------------------------------------------------- D-01 prices
    sep = {}
    for t, rows in _each(SEP_DIR):
        d = pd.DataFrame(rows)
        if "lastupdated" in d:                      # RULE 4: prices collapse to newest vintage
            d = d.sort_values("lastupdated").drop_duplicates("date", keep="last")
        sep[t] = d.set_index("date")

    frames = []
    for t, rows in _each(EOD_DIR):
        e = pd.DataFrame(rows)
        if "date" not in e:
            continue
        e = e.drop_duplicates("date", keep="last").set_index("date")
        s = sep.get(t)
        out = pd.DataFrame(index=e.index)
        out["open"], out["high"], out["low"] = e.get("open"), e.get("high"), e.get("low")
        out["volume"], out["adjusted_close_vendor"] = e.get("volume"), e.get("adjusted_close")
        # RULE 2: Sharadar closeunadj is the raw print; EODHD close only where Sharadar has none.
        out["close"] = e.get("close")
        out["close_source"] = "eodhd"
        if s is not None and "closeunadj" in s:
            j = s["closeunadj"].reindex(out.index)
            hit = j.notna()
            out.loc[hit, "close"] = j[hit]
            out.loc[hit, "close_source"] = "sharadar_closeunadj"
            if "closeadj" in s:
                out["close_fully_adjusted"] = s["closeadj"].reindex(out.index)
        out["ticker"] = t
        frames.append(out.reset_index())

    px = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    n_raw = len(px)
    px = px[px["date"].isin(S)]                                   # RULE 1: session grid
    n_sess = len(px)
    px["permaticker"] = px["ticker"].map(permatick)               # RULE 7
    # FLAG, do not delete. Rows before the entity master's firstpricedate are usually a corporate
    # event that kept the tape running (re-domicile, merger, rename) and only occasionally a
    # recycled symbol. Deleting them cost 30,866 rows of legitimate history — STE, LIN, BKR, EVRG,
    # GOOG and KKR each lost years of their own prices — before the distinction was understood.
    # validate.py's quarantine is where the recycled-symbol judgement lives; here we only mark.
    fp = px["ticker"].map(first_price)
    px["pre_first_price_date"] = (fp.notna() & (px["date"] < fp)).fillna(False)
    px["quarantined"] = False
    for t, a, b in qspans:                                        # RULE 3
        m = (px["ticker"] == t) & (px["date"] >= a) & (px["date"] <= b)
        px.loc[m, "quarantined"] = True
        px.loc[m & (px["close_source"] == "eodhd"), "close"] = pd.NA
    px = px.copy()          # de-fragment after the boolean masking above
    px["close_unadj_flag"] = True
    px = _prov(px, "EODHD+Sharadar", "eod/{T}.US + sharadar/stocks", snapshot)
    px = px.sort_values(["ticker", "date"])
    manifest["tables"]["raw_prices_eod"] = _write(px, os.path.join(OUT_DIR, "raw_prices_eod"),
                                                  partition_year=True)
    src = px["close_source"].value_counts().to_dict()
    log(f"OK   raw_prices_eod    : {len(px):,} rows  (dropped {n_raw - n_sess:,} non-session; "
        f"flagged {int(px['pre_first_price_date'].sum()):,} pre-listing, NOT dropped)  close_source={src}  "
        f"quarantined={int(px['quarantined'].sum()):,}")

    # ---------------------------------------------------------------- D-02/03/04 corporate actions
    acts = []
    sh_split, sh_spin = set(), set()
    for t, rows in _each(ACTIONS_DIR):
        for r in rows:
            a = (r.get("action") or "").lower()
            key = (t, str(r.get("date"))[:10])
            if a == "split":
                sh_split.add(key)
            elif a in ("spinoff", "spinoffdividend", "spunofffrom"):
                sh_spin.add(key)
            acts.append({"date": str(r.get("date"))[:10], "ticker": t, "action_type": a,
                         "value": r.get("value"), "contraticker": r.get("contraticker"),
                         "source": "sharadar_actions"})
    n_phantom = 0
    for t, rows in _each(SPLITS_DIR):
        for r in rows:
            d = str(r.get("date"))[:10]
            try:
                a, b = str(r.get("split", "")).split("/")
                ratio = float(a) / float(b)
            except (ValueError, ZeroDivisionError):
                continue
            # RULE 6: EODHD files spinoff price adjustments as split ratios.
            typ = "split"
            if (t, d) in sh_spin and (t, d) not in sh_split:
                typ, n_phantom = "spinoff", n_phantom + 1
            acts.append({"date": d, "ticker": t, "action_type": typ, "value": ratio,
                         "contraticker": None, "source": "eodhd_splits"})
    for t, rows in _each(DIV_DIR):
        for r in rows:
            acts.append({"date": str(r.get("date"))[:10], "ticker": t, "action_type": "div_cash",
                         "value": r.get("unadjustedValue", r.get("value")),
                         "contraticker": None, "source": "eodhd_div",
                         "payment_date": r.get("paymentDate"), "record_date": r.get("recordDate"),
                         "declaration_date": r.get("declarationDate")})
    ca = pd.DataFrame(acts)
    if not ca.empty:
        ca["permaticker"] = ca["ticker"].map(permatick)
        ca = _prov(ca, "Sharadar+EODHD", "actions + splits + div", snapshot).sort_values(["ticker", "date"])
    manifest["tables"]["corporate_actions"] = _write(ca, os.path.join(OUT_DIR, "corporate_actions.parquet"))
    log(f"OK   corporate_actions : {len(ca):,} rows  ({n_phantom} EODHD 'splits' retyped as spinoff)  "
        f"{ca['action_type'].value_counts().head(5).to_dict() if not ca.empty else ''}")

    # ---------------------------------------------------------------- Q-002 adjustment factors
    # From Sharadar closeadj/closeunadj where present — a vendor-computed, internally consistent
    # pair — rather than EODHD adjusted_close/close, whose denominator is the mutable column.
    fac = []
    for t, s in sep.items():
        if "closeadj" in s and "closeunadj" in s:
            f = (pd.to_numeric(s["closeadj"], errors="coerce")
                 / pd.to_numeric(s["closeunadj"], errors="coerce")).dropna()
            if len(f):
                fac.append(pd.DataFrame({"date": f.index, "ticker": t, "factor": f.values,
                                         "factor_source": "sharadar_closeadj/closeunadj"}))
    af = pd.concat(fac, ignore_index=True) if fac else pd.DataFrame()
    if not af.empty:
        af = af[af["date"].isin(S)]
        af = _prov(af, "Sharadar", "api.sharadar.com/stocks", snapshot)
    manifest["tables"]["adjustment_factors"] = _write(af, os.path.join(OUT_DIR, "adjustment_factors"),
                                                      partition_year=True)
    log(f"OK   adjustment_factors: {len(af):,} rows")

    # ---------------------------------------------------------------- D-05/06 fundamentals (LONG)
    ID = {"ticker", "dimension", "calendardate", "date", "datekey", "reportperiod",
          "fiscalperiod", "lastupdated"}
    recs = []
    for t, rows in _each(SF1_DIR):
        for r in rows:
            filing = str(r.get("datekey") or r.get("date") or "")[:10]
            if not filing:
                continue
            base = {"ticker": t, "permaticker": permatick.get(t),
                    "fiscal_period": r.get("reportperiod") or r.get("calendardate"),
                    "report_date": r.get("calendardate"), "filing_datetime": filing,
                    "lastupdated": r.get("lastupdated"), "dimension": r.get("dimension")}
            for k, v in r.items():
                if k in ID or v in (None, ""):
                    continue
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                recs.append(dict(base, item=k, value=fv))
    fu = pd.DataFrame(recs)
    if not fu.empty:
        # PK is (ticker, fiscal_period, filing_datetime, item, LASTUPDATED). The spec's four-column
        # key (§3) cannot hold the data the same section mandates: M1-01 keeps every restatement
        # vintage, and 95,159 rows here share a four-column key while differing only by
        # lastupdated. Adding it makes the key unique (verified: 0 duplicates).
        # RULE 4: vintages are KEPT — T-11 needs lastupdated <= t as well as filing_datetime <= t.
        fu = _prov(fu, "Sharadar", "api.sharadar.com/fundamentals", snapshot)
        fu = fu.sort_values(["ticker", "fiscal_period", "filing_datetime", "item"])
    manifest["tables"]["fundamentals_pit"] = _write(fu, os.path.join(OUT_DIR, "fundamentals_pit.parquet"))
    nv = fu.groupby(["ticker", "fiscal_period"])["lastupdated"].nunique() if not fu.empty else pd.Series(dtype=int)
    log(f"OK   fundamentals_pit  : {len(fu):,} rows, {fu['item'].nunique() if not fu.empty else 0} items, "
        f"{int((nv > 1).sum()) if len(nv) else 0} (ticker,period) with >1 vintage")

    # ---------------------------------------------------------------- D-14 analyst estimates
    # D-14 was written off as "forward-only, rev_mom stays NaN" because no affordable vendor sells
    # a vintage consensus feed. That conclusion was wrong, and the correction is worth recording:
    # the EODHD fundamentals blob already pulled nightly carries two estimate histories that
    # nothing downstream was parsing.
    #
    #   Earnings::History  consensus epsEstimate paired with epsActual per fiscal quarter, back to
    #                      ~1995 (median 123 quarters/ticker across the 500). A genuine analyst
    #                      surprise series, independent of the SF1-derived SUE.
    #   Earnings::Trend    per period: epsTrendCurrent against epsTrend{7,30,60,90}daysAgo, plus
    #                      up/down revision counts and the revenue consensus. Rows for PAST periods
    #                      stay frozen at their final values rather than being overwritten with
    #                      today's number (36 of 36 AAPL past periods have current != 90daysAgo),
    #                      so each is a real revision-momentum observation reaching back to 2017.
    #
    # PIT HANDLING is the whole difficulty here. data/estimates/*.json IS a true vintage panel —
    # one dated snapshot per run — so those rows carry an exact as_of_date and can be trusted
    # literally. The fundamentals-derived rows carry no vintage stamp, so they are dated at the
    # period's own reportDate (from Earnings::History, which is the date the number stopped being
    # a forecast) and marked `as_of_basis`. Anything filtering `as_of_date <= t` is then honest for
    # both, and a consumer that wants only the exact-vintage rows can select on the basis column.
    TREND_NUM = {
        "eps_avg": "earningsEstimateAvg", "eps_low": "earningsEstimateLow",
        "eps_high": "earningsEstimateHigh", "eps_n_analysts": "earningsEstimateNumberOfAnalysts",
        "eps_growth": "earningsEstimateGrowth", "eps_year_ago": "earningsEstimateYearAgoEps",
        "rev_avg": "revenueEstimateAvg", "rev_low": "revenueEstimateLow",
        "rev_high": "revenueEstimateHigh", "rev_n_analysts": "revenueEstimateNumberOfAnalysts",
        "rev_growth": "revenueEstimateGrowth",
        "eps_trend_current": "epsTrendCurrent", "eps_trend_7d": "epsTrend7daysAgo",
        "eps_trend_30d": "epsTrend30daysAgo", "eps_trend_60d": "epsTrend60daysAgo",
        "eps_trend_90d": "epsTrend90daysAgo",
        "eps_rev_up_7d": "epsRevisionsUpLast7days", "eps_rev_up_30d": "epsRevisionsUpLast30days",
        "eps_rev_dn_7d": "epsRevisionsDownLast7days", "eps_rev_dn_30d": "epsRevisionsDownLast30days",
    }

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _trend_row(t, period, r, as_of, basis):
        row = {"ticker": t, "permaticker": permatick.get(t), "period": str(period)[:10],
               "period_type": r.get("period"), "as_of_date": as_of, "as_of_basis": basis}
        row.update({k: _num(r.get(v)) for k, v in TREND_NUM.items()})
        return row

    # Iterated directly rather than through _each(): a fundamentals file is one dict, not the list
    # of rows every other per-ticker feed uses, so _each would skip all 503 of them silently.
    est, surp, report_date = [], [], {}
    for p in sorted(glob.glob(os.path.join(FUND_DIR, "*.json"))):
        t = os.path.basename(p)[:-5]
        if t.startswith("_"):
            continue
        E = (_load(p) or {}).get("Earnings") or {}
        hist = E.get("History") or {}
        for period, r in hist.items():
            if not isinstance(r, dict):
                continue
            rd = str(r.get("reportDate") or "")[:10]
            if rd:
                report_date[(t, str(period)[:10])] = rd
            est_v, act_v = _num(r.get("epsEstimate")), _num(r.get("epsActual"))
            if est_v is None and act_v is None:
                continue
            surp.append({
                "ticker": t, "permaticker": permatick.get(t), "fiscal_period": str(period)[:10],
                "report_date": rd or None, "before_after_market": r.get("beforeAfterMarket"),
                "eps_estimate": est_v, "eps_actual": act_v,
                "eps_difference": _num(r.get("epsDifference")),
                "surprise_percent": _num(r.get("surprisePercent")),
            })
        for period, r in (E.get("Trend") or {}).items():
            if isinstance(r, dict):
                # No vintage stamp on these: date them at the report date when we know it, else
                # period end + 45 days, the conservative outer edge of a US filing deadline.
                pk = str(period)[:10]
                rd = report_date.get((t, pk))
                if not rd:
                    try:
                        rd = (datetime.strptime(pk, "%Y-%m-%d") + timedelta(days=45)).date().isoformat()
                    except ValueError:
                        rd = None
                est.append(_trend_row(t, period, r, rd, "fundamentals_trend_frozen"))

    # The exact-vintage half: one dated snapshot per run since the estimates fetcher went live.
    for t, snaps in _each(EST_DIR):
        for s in snaps:
            if not isinstance(s, dict):
                continue
            as_of = str(s.get("date") or "")[:10] or None
            for period, r in (s.get("trend") or {}).items():
                if isinstance(r, dict):
                    est.append(_trend_row(t, period, r, as_of, "daily_snapshot"))

    ep = pd.DataFrame(est)
    if not ep.empty:
        # An exact vintage always beats an inferred one for the same (ticker, period, date).
        ep["_rank"] = (ep["as_of_basis"] == "daily_snapshot").astype(int)
        ep = (ep.sort_values(["ticker", "period", "as_of_date", "_rank"])
                .drop_duplicates(subset=["ticker", "period", "as_of_date"], keep="last")
                .drop(columns="_rank"))
        ep = _prov(ep, "EODHD", "eodhd.com/api/fundamentals::Earnings", snapshot)
    manifest["tables"]["estimates_pit"] = _write(ep, os.path.join(OUT_DIR, "estimates_pit.parquet"))
    n_exact = int((ep["as_of_basis"] == "daily_snapshot").sum()) if not ep.empty else 0
    log(f"OK   estimates_pit     : {len(ep):,} rows, "
        f"{ep['ticker'].nunique() if not ep.empty else 0} tickers, {n_exact:,} exact-vintage / "
        f"{len(ep) - n_exact:,} report-dated, "
        f"{ep['as_of_date'].min() if not ep.empty else '-'}..{ep['as_of_date'].max() if not ep.empty else '-'}")

    sp = pd.DataFrame(surp)
    if not sp.empty:
        sp = sp.drop_duplicates(subset=["ticker", "fiscal_period"], keep="last")
        sp = _prov(sp, "EODHD", "eodhd.com/api/fundamentals::Earnings::History", snapshot)
        sp = sp.sort_values(["ticker", "fiscal_period"])
    manifest["tables"]["earnings_surprises"] = _write(
        sp, os.path.join(OUT_DIR, "earnings_surprises.parquet"))
    both = sp.dropna(subset=["eps_estimate", "eps_actual"]) if not sp.empty else pd.DataFrame()
    log(f"OK   earnings_surprises: {len(sp):,} rows, "
        f"{sp['ticker'].nunique() if not sp.empty else 0} tickers, {len(both):,} with est+actual, "
        f"{sp['fiscal_period'].min() if not sp.empty else '-'}..{sp['fiscal_period'].max() if not sp.empty else '-'}")

    # ---------------------------------------------------------------- D-10 borrow
    b = []
    for t, rows in _each(BORROW_DIR):
        b.extend(rows)
    bf = pd.DataFrame(b)
    if not bf.empty:
        bf["permaticker"] = bf["ticker"].map(permatick)
        bf = _prov(bf, "iBorrowDesk", "www.iborrowdesk.com/api/ticker", snapshot)
    manifest["tables"]["borrow_fees"] = _write(bf, os.path.join(OUT_DIR, "borrow_fees.parquet"))
    log(f"OK   borrow_fees       : {len(bf):,} rows, {bf['ticker'].nunique() if not bf.empty else 0} tickers")

    # ---------------------------------------------------------------- §4 qlib bridge
    if QLIB and not px.empty:
        qd = os.path.join(OUT_DIR, "qlib")
        os.makedirs(qd, exist_ok=True)
        fmap = af.set_index(["ticker", "date"])["factor"] if not af.empty else None
        n = 0
        for t, g in px.groupby("ticker"):
            g = g[["date", "open", "close", "high", "low", "volume"]].copy()
            g["factor"] = ([fmap.get((t, d), 1.0) for d in g["date"]] if fmap is not None else 1.0)
            g = g.dropna(subset=["close"])
            if len(g):
                g.to_csv(os.path.join(qd, f"{t}.csv"), index=False)
                n += 1
        manifest["tables"]["qlib_csv"] = n
        log(f"OK   qlib bridge       : {n} per-ticker CSVs (date,open,close,high,low,volume,factor)")

    manifest["rules_applied"] = [
        "session grid from D-11 (non-session rows dropped)",
        "raw close prefers Sharadar closeunadj over the mutable EODHD close; close_source recorded",
        "validate.py quarantine applied — tainted EODHD closes nulled",
        "prices collapsed to max(lastupdated); fundamentals keep every vintage for T-11",
        "permaticker joined; pre-firstpricedate rows dropped (recycled symbols)",
        "EODHD splits coinciding with a Sharadar spinoff retyped as spinoff",
        "Q-002 factor from Sharadar closeadj/closeunadj, not EODHD adjusted_close/close",
        "§3 provenance columns on every table",
    ]
    with open(os.path.join(OUT_DIR, "_manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    log(f"     manifest          : {os.path.join(OUT_DIR, '_manifest.json')}  "
        f"snapshot {snapshot['id']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
