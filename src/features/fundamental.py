"""M4 — F7 fundamental ratios (weeks-to-year horizons, §F), all PIT via M2-03.

PIT basis [IMPL]: as-FIRST-reported values (earliest vintage per (ticker, fiscal_period)),
visible from filing + 1 session — restatements never rewrite feature history (T-11).
TTM = sum of the last 4 as-first-reported quarters, visible when the newest is.
mcap [IMPL]: latest visible SF1 `marketcap` (self-consistent basis, RULE 5 safe);
`size` = ln(mcap).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.pit import next_session

# Sharadar SF1 vocabulary: cost of goods is `cor` (cost of revenue), not `cogs`
ITEMS = ["netinccmn", "equity", "revenue", "cor", "assets", "workingcapital",
         "depamor", "marketcap", "sharesbas", "eps"]


def _first_vintage(fund: pd.DataFrame) -> pd.DataFrame:
    f = fund[fund["item"].isin(ITEMS)].copy()
    f["filing_datetime"] = pd.to_datetime(f["filing_datetime"])
    f["lastupdated"] = pd.to_datetime(f.get("lastupdated"))
    f = f.sort_values(["ticker", "item", "fiscal_period", "lastupdated"])
    return f.groupby(["ticker", "item", "fiscal_period"], as_index=False).first()


def _quarterly(fv: pd.DataFrame, item: str) -> pd.DataFrame:
    """ticker x fiscal_period frame of as-first-reported values + visibility dates."""
    g = fv[fv["item"] == item]
    val = g.pivot_table(index="fiscal_period", columns="ticker", values="value", aggfunc="first")
    vis = g.pivot_table(index="fiscal_period", columns="ticker", values="filing_datetime",
                        aggfunc="first")
    return val, vis


def _as_of(val: pd.DataFrame, vis: pd.DataFrame, dates: pd.DatetimeIndex,
           sessions: pd.DatetimeIndex, tickers: pd.Index) -> pd.DataFrame:
    """Scatter (value, visible_from) events onto the trading grid, forward-filled."""
    out = pd.DataFrame(np.nan, index=dates, columns=tickers)
    for t in val.columns:
        if t not in out.columns:
            continue
        v = val[t].dropna()
        d = next_session(pd.to_datetime(vis[t].reindex(v.index)), sessions)
        ser = pd.Series(v.values, index=d.values).dropna()
        ser = ser[~ser.index.duplicated(keep="last")].sort_index()
        if len(ser):
            out[t] = ser.reindex(dates.union(ser.index)).ffill().reindex(dates)
    return out


def fundamental_features(fund: pd.DataFrame, raw_close: pd.DataFrame,
                         sessions: pd.DatetimeIndex,
                         dimension: str = "ARQ") -> dict[str, pd.DataFrame]:
    """Returns F7 blocks as wide frames + shares_pit for F6 turnover."""
    dates, tickers = raw_close.index, raw_close.columns
    f = fund[fund.get("dimension", pd.Series(dtype=object)).fillna(dimension) == dimension] \
        if "dimension" in fund else fund
    fv = _first_vintage(f)
    if fv.empty:
        nanf = pd.DataFrame(np.nan, index=dates, columns=tickers)
        return {k: nanf.copy() for k in
                ("ep", "bm", "sp", "roe", "gross_prof", "asset_growth", "accruals", "size")} | \
               {"_shares_pit": nanf.copy(), "_mcap": nanf.copy()}

    q: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {i: _quarterly(fv, i) for i in ITEMS}

    def asof(item):
        val, vis = q[item]
        return _as_of(val, vis, dates, sessions, tickers)

    def asof_ttm(item):
        val, vis = q[item]
        return _as_of(val.rolling(4, min_periods=4).sum(), vis, dates, sessions, tickers)

    def asof_lag(item, k):
        val, vis = q[item]
        return _as_of(val.shift(k), vis, dates, sessions, tickers)

    mcap = asof("marketcap")
    ni_ttm, rev_ttm = asof_ttm("netinccmn"), asof_ttm("revenue")
    equity, assets = asof("equity"), asof("assets")
    cogs_ttm = asof_ttm("cor")
    assets_yoy = asof_lag("assets", 4)
    wc, wc_prev = asof("workingcapital"), asof_lag("workingcapital", 1)
    dep = asof("depamor")
    shares = asof("sharesbas")

    pos = lambda x: x.where(x > 0)
    out = {
        "ep": ni_ttm / pos(mcap),
        "bm": pos(equity) / pos(mcap),
        "sp": rev_ttm / pos(mcap),
        "roe": ni_ttm / pos(equity),
        "gross_prof": (rev_ttm - cogs_ttm) / pos(assets),          # Novy-Marx
        "asset_growth": assets / pos(assets_yoy) - 1,
        "accruals": ((wc - wc_prev) - dep) / pos((assets + assets_yoy) / 2),  # Sloan
        "size": np.log(pos(mcap)),
        "_shares_pit": shares,
        "_mcap": mcap,
    }
    return out
