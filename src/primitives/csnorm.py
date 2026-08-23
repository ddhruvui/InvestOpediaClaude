"""M3 — Q-017 cross-sectional transform (C-04): one function, three modes.

Per day, per column, over the universe mask ONLY (M2-02). Also produces model-score
ranks in M10 and Spearman inputs for IC in M16 (C-11).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def cs_transform(x: pd.DataFrame, mask: pd.DataFrame | None = None,
                 mode: str = "rank", winsor: tuple[float, float] = (0.01, 0.99)) -> pd.DataFrame:
    """
    rank   : fractional rank mapped to (-0.5, +0.5)  [IMPL default for features]
    zscore : winsorize at 1st/99th pct within day, then (x-mu)/sigma  [MAY]
    demean : x - cross-sectional mean  (labels default)
    """
    if mask is not None:
        x = x.where(mask)
    if mode == "rank":
        n = x.notna().sum(axis=1)
        r = x.rank(axis=1, method="average")
        return r.sub(0.5).div(n, axis=0) - 0.5
    if mode == "demean":
        return x.sub(x.mean(axis=1), axis=0)
    if mode == "zscore":
        lo = x.quantile(winsor[0], axis=1)
        hi = x.quantile(winsor[1], axis=1)
        xc = x.clip(lower=lo, upper=hi, axis=0)
        return xc.sub(xc.mean(axis=1), axis=0).div(xc.std(axis=1).replace(0, np.nan), axis=0)
    raise ValueError(f"unknown mode {mode!r}")
