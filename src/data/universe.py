"""M2 — Q-003 dated universe mask (G-05).

Liquid US large/mid-cap, survivorship-bias-free, applied BEFORE any
cross-sectional operation. Two modes (BP1):

  top1000_dollar_volume : rank by 63d rolling MEDIAN of raw_close*volume at each
                          monthly refresh; top `size` with a 10% exit-hysteresis
                          buffer; then min_price / min_mcap filters; membership
                          applies from the NEXT session (no same-day inclusion).
  sp500_historical      : interval join on dated index membership.

Out-of-universe rows are absent from cross-sectional ops, never zero-filled (M2-02).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def month_end_sessions(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Last trading session of each calendar month present in `dates`."""
    s = pd.Series(dates, index=dates)
    return pd.DatetimeIndex(s.groupby([dates.year, dates.month]).max().values)


def top_dollar_volume_mask(raw_close: pd.DataFrame, volume: pd.DataFrame,
                           size: int = 1000, window: int = 63, hysteresis: float = 0.10,
                           min_price: float = 5.0, mcap: pd.DataFrame | None = None,
                           min_mcap: float = 300e6) -> pd.DataFrame:
    """Q-003 `top1000_dollar_volume` mode. Returns bool (date x ticker)."""
    dv = raw_close * volume                                   # Q-010
    med = dv.rolling(window, min_periods=window // 2).median()
    dates = raw_close.index
    mask = pd.DataFrame(False, index=dates, columns=raw_close.columns)
    current: set[str] = set()
    refreshes = month_end_sessions(dates)
    # Membership computed at refresh r applies (r, next_refresh] — next session onward.
    for i, r in enumerate(refreshes):
        m = med.loc[r].dropna()
        px = raw_close.loc[r]
        m = m[px.reindex(m.index) >= min_price]
        if mcap is not None:
            mc = mcap.loc[r].reindex(m.index)
            m = m[(mc >= min_mcap) | mc.isna()]               # names w/o mcap data: log-only, keep
        if m.empty:
            continue
        rank = m.rank(ascending=False, method="first")
        entrants = set(rank[rank <= size].index)
        stay = {t for t in current if rank.get(t, np.inf) <= size * (1 + hysteresis)}
        current = entrants | stay
        start = dates.searchsorted(r) + 1                     # next session (no look-ahead)
        end = dates.searchsorted(refreshes[i + 1]) + 1 if i + 1 < len(refreshes) else len(dates)
        if start < len(dates):
            cols = [t for t in current if t in mask.columns]
            mask.iloc[start:end, [mask.columns.get_loc(t) for t in cols]] = True
    # A name is only in-universe on days it actually trades:
    return mask & raw_close.notna()


def interval_mask(intervals: pd.DataFrame, dates: pd.DatetimeIndex,
                  tickers: pd.Index) -> pd.DataFrame:
    """`sp500_historical` mode: intervals(date_start, date_end, ticker) -> bool mask (M1-03)."""
    mask = pd.DataFrame(False, index=dates, columns=tickers)
    for _, r in intervals.iterrows():
        t = r["ticker"]
        if t not in mask.columns:
            continue
        a = pd.to_datetime(r["date_start"])
        b = pd.to_datetime(r["date_end"]) if pd.notna(r["date_end"]) else dates[-1]
        mask.loc[(dates >= a) & (dates <= b), t] = True
    return mask


def sp500_intervals_from_sharadar(sp500_rows: list[dict]) -> pd.DataFrame:
    """Sharadar SP500 table (date, action in {added,removed,current,historical}, ticker)
    -> dated membership intervals, survivorship-free (M1-03)."""
    df = pd.DataFrame(sp500_rows)
    df["date"] = pd.to_datetime(df["date"])
    out = []
    for t, g in df.sort_values("date").groupby("ticker"):
        start = None
        for _, r in g.iterrows():
            a = str(r.get("action", "")).lower()
            if a in ("added", "current", "historical") and start is None:
                start = r["date"]
            elif a == "removed" and start is not None:
                out.append({"ticker": t, "date_start": start, "date_end": r["date"]})
                start = None
        if start is not None:
            out.append({"ticker": t, "date_start": start, "date_end": pd.NaT})
    return pd.DataFrame(out)
