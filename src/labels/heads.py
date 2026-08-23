"""M5.1 — horizon-head labels (§F, BP4):  y_n(t,i) = CSnorm_t[ fwd_n(t,i) ],
n in {5,20,60}. fwd_n comes from Q-016 (lag baked in THERE and only there)."""
from __future__ import annotations

import pandas as pd

from src.primitives.csnorm import cs_transform
from src.primitives.fwd import forward_return


def horizon_labels(adj_close: pd.DataFrame, mask: pd.DataFrame,
                   horizons=(5, 20, 60), cs_norm: str = "demean") -> dict[int, pd.DataFrame]:
    return {n: cs_transform(forward_return(adj_close, n), mask, mode=cs_norm)
            for n in horizons}
