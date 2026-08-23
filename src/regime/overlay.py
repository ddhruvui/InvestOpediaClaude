"""M13 — regime overlay (BP13, §K): gross_mult(t) in [0.5, 1.0].

Default rule [MUST]: 0.5 if SPY_adjclose < SMA200 OR realized 21d ann vol > 0.25,
else 1.0. HMM alternative [MAY] fits ON THE TRAINING FOLD ONLY (no full-sample
smoothing leak), uses FILTERED probabilities, maps 0.5 + 0.5*P(calm).
The momentum sleeve's own vol-scaling is its regime defense — no double-apply.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def regime_multiplier(index_block: dict, vol_threshold: float = 0.25,
                      risk_off_mult: float = 0.5) -> pd.Series:
    spy, sma, vol = (index_block["spy_adj_close"], index_block["sma200"],
                     index_block["vol21_ann"])
    risk_off = (spy < sma) | (vol > vol_threshold)
    return pd.Series(np.where(risk_off, risk_off_mult, 1.0), index=spy.index,
                     name="gross_mult")
