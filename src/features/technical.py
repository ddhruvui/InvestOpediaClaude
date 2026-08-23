"""M4 — technical feature blocks F1-F6 (§F). All on adj_* (G-03); every windowed
stat comes from the Q-015 RollingCache (M4-03) — no ad-hoc rolling loops."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.primitives.monthly import mom_12_1
from src.primitives.rolling import RollingCache


def wilder_ema(x: pd.DataFrame, period: int) -> pd.DataFrame:
    """Wilder smoothing == EMA with alpha=1/period (canonical [IMPL]; audit Pass B)."""
    return x.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def iter_technical_features(panel, r: pd.DataFrame, tr: pd.DataFrame,
                            cache: RollingCache,
                            shares_pit: pd.DataFrame | None = None):
    """Yields (feature_name, wide frame) for F1-F6 ONE AT A TIME, un-normalized —
    holding the whole block in memory peaks ~4 GB at a 4k-name workset."""
    C, O, Hh, Ll, V = (panel.adj_close, panel.adj_open, panel.adj_high, panel.adj_low,
                       panel.adj_volume)

    # ---- F1 price/return ----
    for w in (1, 5, 10, 20, 60):
        yield f"ret_{w}", C / C.shift(w) - 1
    yield "co_ratio", C / O
    yield "hl_ratio", Hh / Ll
    rng = Hh - Ll
    yield "range_pos", ((C - Ll) / rng).where(rng > 0, 0.5)   # 0.5 when H=L [IMPL]
    del rng

    # ---- F2 moving averages, as ratios ----
    for w in (5, 10, 20, 30, 60):
        yield f"close_sma_{w}", C / cache.get("adj_close", "mean", w)
        yield f"close_ema_{w}", C / cache.get("adj_close", "ema", w)

    # ---- F3 momentum ----
    yield "mom_12_1", mom_12_1(C)                              # Q-018 (C-09)
    delta = C.diff()
    rs = wilder_ema(delta.clip(lower=0), 14) / wilder_ema((-delta).clip(lower=0), 14)
    yield "rsi_14", 100 - 100 / (1 + rs)
    del delta, rs

    # ---- F4 MACD 12/26/9, scale-free (/close) [IMPL] ----
    macd = C.ewm(span=12, adjust=False).mean() - C.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    yield "macd", macd / C
    yield "macd_signal", signal / C
    yield "macd_hist", (macd - signal) / C
    del macd, signal

    # ---- F5 volatility ----
    for w in (5, 10, 20, 60):
        yield f"vol_{w}", cache.get("r", "std", w)
    yield "atr_14", wilder_ema(tr, 14) / C
    sma20, std20 = cache.get("adj_close", "mean", 20), cache.get("adj_close", "std", 20)
    yield "bb_pos", (C - sma20) / (2 * std20)                  # %B-affine (F5)

    # ---- F6 volume ----
    vma20 = cache.get("volume", "mean", 20)
    yield "vma_ratio", V / vma20
    if shares_pit is not None:
        # RAW volume over as-reported shares: both legs share the then-current basis
        yield "turnover", panel.raw_volume / shares_pit
    yield "vwap_dev", C / ((Hh + Ll + C) / 3) - 1              # VWAP proxy [IMPL]
    obv = (np.sign(r.fillna(0)) * V.fillna(0)).cumsum()
    for w in (5, 20):
        yield f"obv_chg_{w}", (obv - obv.shift(w)) / vma20
    del obv


def technical_features(panel, r, tr, cache, shares_pit=None) -> dict[str, pd.DataFrame]:
    """Dict wrapper kept for tests/small panels."""
    return dict(iter_technical_features(panel, r, tr, cache, shares_pit))
