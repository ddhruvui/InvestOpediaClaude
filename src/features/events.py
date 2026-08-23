"""M4 — F8 event block (§F): SUE, PEAD carry, estimate-revision momentum,
earnings-distance features. All PIT: an announcement is visible at close of its
session if before-market, else from the NEXT session (EOD cutoff, M4-01).

SUE [IMPL]: est_stddev is not in the vendor history, so the primary form is
(actual - consensus) / sigma(last 8 surprises); when no consensus exists the
spec's canonical fallback applies: seasonal-diff SUE = (EPS_q - EPS_{q-4}) /
sigma(last 8 seasonal diffs).
rev_mom [IMPL scaling]: (eps_trend_current - eps_trend_90d) / raw_close — both
legs inside one estimates_pit row, so the split-basis caveat (RULE 9) is safe.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.pit import next_session

CAP = 63  # days_to/since capped [§F]


def _announce_session(report_date: pd.Series, bam: pd.Series,
                      sessions: pd.DatetimeIndex) -> pd.Series:
    """Session at whose close the announcement is first known."""
    d = pd.to_datetime(report_date)
    before = bam.astype(str).str.lower().str.startswith("before")
    idx = np.searchsorted(sessions.values, d.values, side="left")
    idx = np.clip(idx, 0, len(sessions) - 1)
    same = pd.Series(sessions.values[idx], index=d.index)
    nxt = next_session(d, sessions)
    return same.where(before & (same.values == d.values), nxt)


def event_features(surprises: pd.DataFrame, estimates: pd.DataFrame,
                   raw_close: pd.DataFrame, sessions: pd.DatetimeIndex,
                   pead_window: int = 60) -> dict[str, pd.DataFrame]:
    dates, tickers = raw_close.index, raw_close.columns
    nanf = lambda: pd.DataFrame(np.nan, index=dates, columns=tickers)
    sue_f, since_f, to_f = nanf(), nanf(), nanf()

    sp = surprises[surprises["ticker"].isin(tickers)].copy() if len(surprises) else surprises
    if len(sp):
        sp["ann"] = _announce_session(sp["report_date"], sp.get("before_after_market"), sessions)
        sp = sp.dropna(subset=["ann"]).sort_values(["ticker", "fiscal_period"])
        # SUE per announcement
        g = sp.groupby("ticker")
        sp["_surp"] = sp["eps_actual"] - sp["eps_estimate"]
        sp["_surp_sig"] = g["_surp"].transform(lambda s: s.rolling(8, min_periods=4).std().shift(1))
        seas = g["eps_actual"].transform(lambda s: s.diff(4))
        sp["_seas"] = seas
        sp["_seas_sig"] = g["_seas"].transform(lambda s: s.rolling(8, min_periods=4).std().shift(1))
        with np.errstate(all="ignore"):
            sue = (sp["_surp"] / sp["_surp_sig"]).where(
                sp["eps_estimate"].notna(), sp["_seas"] / sp["_seas_sig"])
        sp["sue"] = sue.replace([np.inf, -np.inf], np.nan)

        for t, gt in sp.groupby("ticker"):
            ev = gt.dropna(subset=["ann"]).drop_duplicates("ann", keep="last")
            anns = pd.DatetimeIndex(ev["ann"])
            if not len(anns):
                continue
            # days_since: sessions since the last announcement (0 on the day itself)
            pos = np.searchsorted(dates.values, anns.values, side="left")
            pos = pos[pos < len(dates)]
            marks = np.full(len(dates), np.nan)
            marks[pos] = pos
            last = pd.Series(marks).ffill().values
            since = np.arange(len(dates)) - last
            since_f[t] = np.minimum(since, CAP)
            # days_to: sessions until the next announcement
            nxt = pd.Series(np.where(np.isin(np.arange(len(dates)), pos),
                                     np.arange(len(dates)), np.nan)).bfill().values
            to_f[t] = np.minimum(nxt - np.arange(len(dates)), CAP)
            # PEAD carry: latest SUE while days_since <= 60, else 0
            sue_ser = pd.Series(np.nan, index=dates)
            sue_ser.iloc[pos] = ev["sue"].values[: len(pos)]
            carried = sue_ser.ffill()
            sue_f[t] = carried.where(pd.Series(since, index=dates) <= pead_window, 0.0).fillna(0.0)

    # rev_mom from estimates_pit daily snapshots + frozen trend rows
    rev_f = nanf()
    est = estimates
    if est is not None and len(est):
        e = est[(est["ticker"].isin(tickers))
                & (est["period_frequency"].isin(["quarterly", "annual"]))].copy()
        e = e.dropna(subset=["as_of_date", "eps_trend_current", "eps_trend_90d"])
        if len(e):
            e["as_of_date"] = pd.to_datetime(e["as_of_date"])
            # nearest FUTURE period per (ticker, as_of): the FY1/FQ1 consensus row
            e["period"] = pd.to_datetime(e["period"])
            e = e[e["period"] >= e["as_of_date"]]
            e = (e.sort_values(["ticker", "as_of_date", "period"])
                   .drop_duplicates(subset=["ticker", "as_of_date"], keep="first"))
            e["chg"] = e["eps_trend_current"] - e["eps_trend_90d"]
            w = e.pivot(index="as_of_date", columns="ticker", values="chg")
            w = w.reindex(dates.union(w.index)).sort_index().ffill(limit=95).reindex(dates)
            rev_f = (w.reindex(columns=tickers) / raw_close).replace([np.inf, -np.inf], np.nan)

    return {
        "sue_pead": sue_f,                       # PEAD carry (=0 outside the 60d window)
        "rev_mom": rev_f,
        "days_to_earnings": to_f,
        "days_since_earnings": since_f,
        "earnings_within_2d": (to_f <= 2).astype(float).where(to_f.notna()),
    }
