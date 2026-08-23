"""M3 — Q-019 index block (C-10): SPY total-return, SMA200, 21d realized vol
(annualized), rolling 60d per-name betas. Consumers: M13 regime, M14 hedge,
M16 SPY baseline, HMM inputs."""
from __future__ import annotations

import numpy as np
import pandas as pd


def index_block(spy_adj_close: pd.Series, r_panel: pd.DataFrame | None = None,
                beta_window: int = 60) -> dict:
    spy_r = spy_adj_close / spy_adj_close.shift(1) - 1
    sma200 = spy_adj_close.rolling(200, min_periods=100).mean()
    vol21 = spy_r.rolling(21, min_periods=15).std() * np.sqrt(252)
    out = {"spy_adj_close": spy_adj_close, "spy_r": spy_r, "sma200": sma200,
           "vol21_ann": vol21}
    if r_panel is not None:
        # closed-form rolling beta: cov = E[xy] - E[x]E[y]; pandas' pairwise
        # rolling cov on a wide frame allocates several full panels (OOM at 4k names)
        mp = beta_window // 2
        x = r_panel.astype("float32")
        y = spy_r.astype("float32")
        exy = x.mul(y, axis=0).rolling(beta_window, min_periods=mp).mean()
        ex = x.rolling(beta_window, min_periods=mp).mean()
        ey = y.rolling(beta_window, min_periods=mp).mean()
        cov = exy.sub(ex.mul(ey, axis=0))
        var = y.rolling(beta_window, min_periods=mp).var() * (beta_window - 1) / beta_window
        out["beta"] = cov.div(var, axis=0)
        del exy, ex, ey, cov
    return out
