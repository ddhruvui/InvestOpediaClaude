"""M3 — Q-018 monthly grid + mom_12_1 (C-09): ONE computation, THREE consumers —
M4 momentum feature, M12 sleeve, M16 baseline."""
from __future__ import annotations

import pandas as pd


def mom_12_1(adj_close: pd.DataFrame) -> pd.DataFrame:
    """Cumulative (t-12m, t-1m] return, session-count form [IMPL: 21/252 sessions]:
    adj_close_{t-21} / adj_close_{t-252} - 1 (skip most recent month)."""
    return adj_close.shift(21) / adj_close.shift(252) - 1


def monthly_returns(adj_close: pd.DataFrame) -> pd.DataFrame:
    """Month-end adjusted closes -> monthly total-return matrix."""
    me = adj_close.groupby([adj_close.index.year, adj_close.index.month]).tail(1)
    return me / me.shift(1) - 1
