"""M2-03 — point-in-time visibility for fundamentals & estimates.

A filing becomes visible at `filing_datetime + 1 trading session` (§F lag).
T-11 additionally requires the VINTAGE filter: a row is only usable at t if
BOTH filing_datetime <= t-1-session AND lastupdated <= t (restatements arrive
as new rows and must not rewrite history).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def next_session(dates: pd.Series | pd.DatetimeIndex, sessions: pd.DatetimeIndex) -> pd.Series:
    """First trading session STRICTLY AFTER each date (M2-03 visible_from)."""
    d = pd.to_datetime(pd.Series(np.asarray(dates)))
    idx = np.searchsorted(sessions.values, d.values, side="right")
    idx = np.clip(idx, 0, len(sessions) - 1)
    out = pd.Series(sessions.values[idx], index=d.index)
    out[idx >= len(sessions) - 1] = pd.NaT   # beyond calendar horizon: never visible in-sample
    return out


def visible_fundamentals(fund: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Attach visible_from; keep every vintage (RULE 4 / T-11)."""
    f = fund.copy()
    f["filing_datetime"] = pd.to_datetime(f["filing_datetime"])
    f["lastupdated"] = pd.to_datetime(f.get("lastupdated", f["filing_datetime"]))
    f["visible_from"] = next_session(f["filing_datetime"], sessions)
    # A restatement can only be seen once the vendor published it:
    f["visible_from"] = f[["visible_from", "lastupdated"]].max(axis=1)
    return f.dropna(subset=["visible_from"])


def pit_series(fund_visible: pd.DataFrame, item: str, dates: pd.DatetimeIndex,
               tickers: pd.Index, dimension: str = "ARQ") -> pd.DataFrame:
    """Wide (date x ticker) as-of series of one SF1 item under PIT rules.

    For each date, the value from the row with the greatest (visible_from, fiscal_period)
    among rows visible by then — i.e., latest filing, latest vintage, no lookahead.
    """
    f = fund_visible[(fund_visible["item"] == item)
                     & (fund_visible["dimension"].fillna(dimension) == dimension)
                     & fund_visible["ticker"].isin(tickers)]
    if f.empty:
        return pd.DataFrame(np.nan, index=dates, columns=tickers)
    f = f.sort_values(["ticker", "visible_from", "fiscal_period", "lastupdated"])
    # last row per (ticker, visible_from) wins, then forward-fill along the calendar
    last = f.groupby(["ticker", "visible_from"], as_index=False).last()
    w = last.pivot(index="visible_from", columns="ticker", values="value")
    w = w.reindex(dates.union(w.index)).sort_index().ffill().reindex(dates)
    return w.reindex(columns=tickers)
