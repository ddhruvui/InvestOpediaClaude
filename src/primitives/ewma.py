"""M3 — Q-014 EWMA volatility family (C-02).

sigma^2_t = lambda*sigma^2_{t-1} + (1-lambda)*lr_t^2,  lambda = 1 - 2/(span+1).
span 32 -> lambda ~= 0.9394 ~ spec's 0.94. `sigma32` is THE shared sigma: barrier
widths (M5), inverse-vol sizing (M14), vol context features (M4/M11). The separate
252-half-life EWM std exists only for M9.2 winsorization (computed on demand).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def span_to_lambda(span: int) -> float:
    return 1.0 - 2.0 / (span + 1.0)


def ewma_sigma(lr: pd.DataFrame, span: int = 32) -> pd.DataFrame:
    """Daily EWMA sigma of log returns. ewm(span, adjust=False).mean() of lr^2 IS the
    recursion above (alpha = 1-lambda = 2/(span+1))."""
    var = (lr ** 2).ewm(span=span, adjust=False, min_periods=max(5, span // 4)).mean()
    return np.sqrt(var)


def ewm_std_halflife(x: pd.DataFrame, halflife: float = 252.0) -> pd.DataFrame:
    """252-day half-life EWM std — ONLY for the Sharpe-loss net's 5-sigma winsorization (§D)."""
    return x.ewm(halflife=halflife, adjust=False, min_periods=20).std()
