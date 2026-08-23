"""M3 — Q-016 forward-return engine (C-05).

THE ONLY HOME OF THE +1 LAG (G-02 / M3-01). Every label consumer imports this
function; the string `shift(-` may appear in M3 and M5 only (T-01 guards).

    fwd_n(t) = adj_close_{t+1+n} / adj_close_{t+1} - 1
"""
from __future__ import annotations

import pandas as pd


def forward_return(adj_close: pd.DataFrame, n: int) -> pd.DataFrame:
    """Lagged n-session forward return, indexed at signal date t."""
    entry = adj_close.shift(-1)          # close of t+1 — first close after the signal
    exit_ = adj_close.shift(-(1 + n))    # close of t+1+n
    return exit_ / entry - 1
