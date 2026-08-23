import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def rng():
    return np.random.default_rng(20260717)


@pytest.fixture
def synth_panel(rng):
    """Small synthetic panel with realistic mechanics (400 sessions x 40 names)."""
    from src.data.panel import Panel
    idx = pd.date_range('2022-01-03', periods=400, freq='B')
    n = 40
    tick = [f'T{i:02d}' for i in range(n)]
    close = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0003, .02, (400, n)), axis=0)),
                         index=idx, columns=tick)
    op = close.shift(1).bfill() * (1 + rng.normal(0, .004, (400, n)))
    hi = pd.DataFrame(np.maximum(close.values, op.values) * (1 + np.abs(rng.normal(0, .004, (400, n)))),
                      index=idx, columns=tick)
    lo = pd.DataFrame(np.minimum(close.values, op.values) * (1 - np.abs(rng.normal(0, .004, (400, n)))),
                      index=idx, columns=tick)
    vol = pd.DataFrame(rng.integers(int(1e5), int(1e7), (400, n)).astype(float), index=idx, columns=tick)
    return Panel(sessions=idx, adj_open=op, adj_high=hi, adj_low=lo, adj_close=close,
                 adj_volume=vol, raw_volume=vol, raw_close=close, raw_open=op,
                 quarantined=pd.DataFrame(False, index=idx, columns=tick))
