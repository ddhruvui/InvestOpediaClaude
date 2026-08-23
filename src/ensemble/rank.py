"""M10 — ensembling & score post-processing (BP7-BP9).

Per member, per day: scores -> cross-sectional fractional ranks (Q-017 rank mode,
C-11). Ensemble = equal-weight mean of member ranks [IMPL]; invariant to monotone
member-score transforms (T-06). Deciles cut per day over the masked universe;
decile 10 = top (M10-04).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.primitives.csnorm import cs_transform


def member_ranks(scores: dict[str, pd.DataFrame], mask: pd.DataFrame) -> dict[str, pd.DataFrame]:
    return {name: cs_transform(s, mask, mode="rank") for name, s in scores.items()}


def ensemble_rank(scores: dict[str, pd.DataFrame], mask: pd.DataFrame,
                  weights: dict[str, float] | None = None) -> pd.DataFrame:
    """Weighted mean of per-day member ranks; equal weights by default (M10-02)."""
    ranks = member_ranks(scores, mask)
    if weights is None:
        weights = {k: 1.0 for k in ranks}
    tot = sum(weights.values())
    acc = None
    for k, r in ranks.items():
        w = weights.get(k, 0.0) / tot
        acc = r * w if acc is None else acc.add(r * w, fill_value=0.0)
    return acc.where(mask)


def deciles(rank_frame: pd.DataFrame, mask: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Per-day decile of ensemble_rank over the masked universe; 10 = top (M10-04)."""
    x = rank_frame.where(mask)
    def cut(row):
        v = row.dropna()
        if len(v) < n:
            return row * np.nan
        # rank(method='first') before qcut: LGBM's L1 regularization emits few
        # distinct leaf values, and raw qcut with duplicates='drop' can lose the
        # top bin entirely (empty book). First-rank tie-break is deterministic
        # (G-10) and monotone-invariant (T-06).
        q = pd.qcut(v.rank(method="first"), n, labels=False) + 1
        return q.reindex(row.index)
    return x.apply(cut, axis=1)
