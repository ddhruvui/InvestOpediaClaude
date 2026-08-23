"""M12.1 — cross-sectional momentum sleeve (§K, BP10) + the G-08 plain baseline.

ONE 12-1 computation (Q-018/C-09), three consumers: M4 feature, this sleeve, the
M16 baseline. `plain=True` produces the mandatory free baseline: unscaled decile
long-short, monthly rebalance, equal weight — same code path (dual duty).

Sleeve: decile sort at month-end close, execute next open (G-02 applies);
overlapping 1/K tranches (K=12 default [IMPL]); LONG-ONLY default (tax tilt);
Barroso–Santa-Clara vol scaling: exposure x min(vol_target / sigma_hat(126d), cap).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.primitives.monthly import mom_12_1
from src.data.universe import month_end_sessions


def momentum_targets(adj_close: pd.DataFrame, mask: pd.DataFrame,
                     K: int = 12, long_only: bool = True, plain: bool = False,
                     n_deciles: int = 10, raw_close: pd.DataFrame | None = None,
                     min_price: float = 5.0) -> pd.DataFrame:
    """Daily target-weight frame of the sleeve BEFORE vol scaling (gross = 1).

    Each month-end a new 1/K tranche opens in the current top decile (long) and,
    if long-short, bottom decile (short); the K-month-old tranche closes.
    plain=True: K=1 (pure monthly rebalance), equal weight, always long-short.
    """
    mom = mom_12_1(adj_close)
    dates = adj_close.index
    rebals = month_end_sessions(dates)
    if plain:
        K, long_only = 1, False
    tranches: list[pd.Series] = []
    snap_rows, snap_dates = [], []
    for r in rebals:
        m = mom.loc[r].where(mask.loc[r]).dropna()
        if raw_close is not None:       # refresh-stale mask: re-screen price at formation
            m = m[raw_close.loc[r].reindex(m.index) >= min_price]
        if len(m) < n_deciles * 2:
            tranche = pd.Series(dtype=float)
        else:
            q = pd.qcut(m.rank(method="first"), n_deciles, labels=False)
            top = m.index[q == n_deciles - 1]
            bot = m.index[q == 0]
            tranche = pd.Series(0.0, index=m.index)
            tranche.loc[top] = 1.0 / len(top)
            if not long_only:
                tranche.loc[bot] = -1.0 / len(bot)
        tranches.append(tranche)
        if len(tranches) > K:
            tranches.pop(0)
        active = (pd.concat(tranches, axis=1).sum(axis=1) / K) if tranches else pd.Series(dtype=float)
        # applies from the NEXT session after the rebalance close (G-02)
        i = dates.searchsorted(r) + 1
        if i < len(dates):
            snap_rows.append(active)
            snap_dates.append(dates[i])
    if not snap_rows:
        return pd.DataFrame(0.0, index=dates, columns=adj_close.columns)
    snaps = pd.DataFrame(snap_rows, index=pd.DatetimeIndex(snap_dates)) \
        .reindex(columns=adj_close.columns).fillna(0.0)
    w = snaps.reindex(dates).ffill().fillna(0.0)     # constant between rebalances
    return w


def vol_scale(sleeve_returns: pd.Series, target: float = 0.12, cap: float = 2.0,
              window: int = 126) -> pd.Series:
    """Barroso–Santa-Clara scale = target / sigma_hat(trailing 6m sleeve vol), capped;
    shifted one day so scaling uses information available at the close before fill."""
    sig = sleeve_returns.rolling(window, min_periods=window // 2).std() * np.sqrt(252)
    return (target / sig).clip(upper=cap).shift(1)
