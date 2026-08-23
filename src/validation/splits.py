"""M16.1 — the ONE purge+embargo splitter (C-07, G-06).

Random k-fold on time series is forbidden. Every split — walk-forward, CPCV, and
every Optuna objective — passes through here.

    purge : drop any train date whose label window [t+1, t+1+span] (session
            indices) overlaps any test interval
    embargo: additionally drop train dates within `embargo` sessions AFTER each
            test interval

CPCV is fixed at N=6, k=2 -> C(6,2)=15 splits; each group appears in exactly
k*C(N,k)/N = 5 test instances -> 5 assembled backtest paths (T-15).
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Fold:
    train: pd.DatetimeIndex
    valid: pd.DatetimeIndex          # empty for CPCV splits
    test: pd.DatetimeIndex
    tag: str = ""


def _purge_embargo(dates: pd.DatetimeIndex, candidate: np.ndarray,
                   test_intervals: list[tuple[int, int]], label_span: int,
                   embargo: int) -> np.ndarray:
    """candidate: sorted positional indices of prospective train dates."""
    keep = np.ones(len(candidate), dtype=bool)
    for a, b in test_intervals:
        # label window of train date i is [i+1, i+1+span]
        overlap = (candidate + 1 <= b) & (candidate + 1 + label_span >= a)
        emb = (candidate > b) & (candidate <= b + embargo)
        keep &= ~(overlap | emb)
    return candidate[keep]


def walk_forward(dates: pd.DatetimeIndex, train_sessions: int = 1260,
                 valid_sessions: int = 504, test_sessions: int = 252,
                 step_sessions: int = 252, label_span: int = 61,
                 embargo: int = 21) -> list[Fold]:
    """Default: train ~5y -> valid ~2y -> test 1y, step 1y [IMPL step].
    FK alternative (§B): call with 750/0/250/250."""
    folds = []
    n = len(dates)
    start = 0
    k = 0
    while True:
        i_tr0 = start
        i_va0 = i_tr0 + train_sessions
        i_te0 = i_va0 + valid_sessions
        i_te1 = i_te0 + test_sessions
        if i_te1 > n:
            break
        test_iv = [(i_te0, i_te1 - 1)]
        tr = _purge_embargo(dates, np.arange(i_tr0, i_va0), test_iv, label_span, embargo)
        va = _purge_embargo(dates, np.arange(i_va0, i_te0), test_iv, label_span, embargo)
        # purge train against valid too — valid steers early stopping (G-06)
        tr = _purge_embargo(dates, tr, [(i_va0, i_te0 - 1)], label_span, 0)
        folds.append(Fold(train=dates[tr], valid=dates[va],
                          test=dates[np.arange(i_te0, i_te1)], tag=f"wf{k}"))
        start += step_sessions
        k += 1
    return folds


def cpcv(dates: pd.DatetimeIndex, n_groups: int = 6, k_test: int = 2,
         label_span: int = 61, embargo: int = 21) -> tuple[list[Fold], list[list[int]]]:
    """Combinatorial purged CV. Returns (splits, path_map) where path_map[p] lists,
    for each group g in order, WHICH split index supplies group g's test slice on
    path p (5 complete paths for 6/2)."""
    n = len(dates)
    bounds = np.linspace(0, n, n_groups + 1).astype(int)
    groups = [np.arange(bounds[g], bounds[g + 1]) for g in range(n_groups)]
    combos = list(combinations(range(n_groups), k_test))
    splits = []
    for ci, combo in enumerate(combos):
        test_iv = [(int(groups[g][0]), int(groups[g][-1])) for g in combo]
        test_idx = np.concatenate([groups[g] for g in combo])
        train_candidate = np.setdiff1d(np.arange(n), test_idx)
        tr = _purge_embargo(dates, train_candidate, test_iv, label_span, embargo)
        splits.append(Fold(train=dates[tr], valid=pd.DatetimeIndex([]),
                           test=dates[test_idx], tag=f"cpcv{ci}:{combo}"))
    # Path assembly: group g's j-th test appearance (in combo order) -> path j.
    n_paths = k_test * len(combos) // n_groups
    appearance: dict[int, int] = {g: 0 for g in range(n_groups)}
    path_map = [[-1] * n_groups for _ in range(n_paths)]
    for ci, combo in enumerate(combos):
        for g in combo:
            path_map[appearance[g]][g] = ci
            appearance[g] += 1
    assert all(all(x >= 0 for x in row) for row in path_map)
    return splits, path_map


def holdout_split(dates: pd.DatetimeIndex, holdout_sessions: int = 252
                  ) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """Final untouched segment, opened exactly once after all selection is frozen."""
    return dates[:-holdout_sessions], dates[-holdout_sessions:]
