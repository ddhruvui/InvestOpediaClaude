"""M8.1 — JKX chart-image renderer (§C). ONE renderer, golden-tested (T-13).

Grayscale, BLACK background (0), WHITE marks (255); prices rescaled PER IMAGE to
fill the price area; volume bars occupy the BOTTOM FIFTH of image height; moving
average line with window = image length, one pixel per day.

Sizes (W x H): 5d = 15x32, 20d = 60x64, 60d = 96... 180x96 — 3 px per day.
Per-day rendering [IMPL canonical JKX]: each day = 3 columns — open tick (left),
high-low bar (center), close tick (right); MA pixel in the center column.
Missing high/low -> draw whatever is available (§C).
"""
from __future__ import annotations

import numpy as np

SIZES = {5: (15, 32), 20: (60, 64), 60: (180, 96)}   # (W, H)


def render_image(opens, highs, lows, closes, volumes, ma) -> np.ndarray:
    """All inputs 1-D arrays of length `days` (NaN allowed). Returns (H, W) uint8."""
    days = len(closes)
    W, H = SIZES[days]
    img = np.zeros((H, W), dtype=np.uint8)
    vol_h = H // 5                                   # bottom fifth
    price_h = H - vol_h - 1                          # one separator row
    px = np.concatenate([np.asarray(x, dtype=float) for x in (opens, highs, lows, closes, ma)])
    finite = px[np.isfinite(px)]
    if finite.size == 0:
        return img
    lo_p, hi_p = float(finite.min()), float(finite.max())
    rng = hi_p - lo_p

    def row(p) -> int | None:
        if not np.isfinite(p):
            return None
        frac = 0.5 if rng == 0 else (p - lo_p) / rng
        return int(round((price_h - 1) * (1 - frac)))

    vmax = np.nanmax(volumes) if np.isfinite(volumes).any() else 0
    for d in range(days):
        c0 = 3 * d                                   # open | bar | close columns
        r = row(opens[d])
        if r is not None:
            img[r, c0] = 255
        rh, rl = row(highs[d]), row(lows[d])
        if rh is not None and rl is not None:
            img[rh:rl + 1, c0 + 1] = 255
        elif rh is not None:
            img[rh, c0 + 1] = 255
        elif rl is not None:
            img[rl, c0 + 1] = 255
        r = row(closes[d])
        if r is not None:
            img[r, c0 + 2] = 255
        r = row(ma[d])
        if r is not None:
            img[r, c0 + 1] = 255                     # MA pixel, center column
        if vmax and np.isfinite(volumes[d]) and volumes[d] > 0:
            vh = int(round((vol_h - 1) * volumes[d] / vmax)) + 1
            img[H - vh:H, c0 + 1] = 255              # volume bar, center column
    return img


def render_windows(adj_open, adj_high, adj_low, adj_close, adj_volume,
                   dates, ticker_cols, days: int = 5) -> dict:
    """Batch: for each date t (window ending AT t), per ticker -> image. Returns
    {date: (imgs uint8 (n, H, W), tickers)}. MA window = image length (days)."""
    import pandas as pd
    ma = adj_close.rolling(days, min_periods=days).mean()
    out = {}
    idx = adj_close.index
    for d in dates:
        i = idx.searchsorted(d)
        if i >= len(idx) or idx[i] != d or i + 1 < days:
            continue
        sl = slice(i - days + 1, i + 1)
        imgs, names = [], []
        O, Hh, L, C = (adj_open.iloc[sl], adj_high.iloc[sl], adj_low.iloc[sl],
                       adj_close.iloc[sl])
        V, M = adj_volume.iloc[sl], ma.iloc[sl]
        for t in ticker_cols:
            c = C[t].to_numpy()
            if np.isfinite(c).sum() < days:          # incomplete window: skip name
                continue
            imgs.append(render_image(O[t].to_numpy(), Hh[t].to_numpy(), L[t].to_numpy(),
                                     c, V[t].to_numpy(), M[t].to_numpy()))
            names.append(t)
        if imgs:
            out[d] = (np.stack(imgs), names)
    return out
