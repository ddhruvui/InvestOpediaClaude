"""M3 — return/range primitives (Q-010..Q-013, Q-020). All inputs are wide adj_* frames (G-03)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def dollar_volume(raw_close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """Q-010: dv_t = raw_close_t * volume_t (raw world — a real-dollar quantity)."""
    return raw_close * volume


def daily_return(adj_close: pd.DataFrame) -> pd.DataFrame:
    """Q-011: r_t = adj_close_t / adj_close_{t-1} - 1. Base series for everything (C-03)."""
    return adj_close / adj_close.shift(1) - 1


def open_to_open_return(adj_open: pd.DataFrame) -> pd.DataFrame:
    """Q-012: oo_t = adj_open_{t+1} / adj_open_t - 1, INDEXED AT t+1 (the day the return
    accrues). Backtest P&L currency (G-02, §H): a position established at open t+1 earns
    row t+2 first. Implemented as a PAST-looking pct change so no forward shift leaks."""
    return adj_open / adj_open.shift(1) - 1


def log_return(r: pd.DataFrame) -> pd.DataFrame:
    """Q-013: lr = ln(1+r) for vol-estimation stability [IMPL]."""
    return np.log1p(r)


def true_range(adj_high: pd.DataFrame, adj_low: pd.DataFrame,
               adj_close: pd.DataFrame) -> pd.DataFrame:
    """Q-020: TR = max(H-L, |H-C_prev|, |L-C_prev|)."""
    pc = adj_close.shift(1)
    a = adj_high - adj_low
    b = (adj_high - pc).abs()
    c = (adj_low - pc).abs()
    return pd.concat({"a": a, "b": b, "c": c}).groupby(level=1).max()
