"""M5.2 — the triple-barrier engine (C-06, §G).

ONE implementation, three call sites (G-15): labeler (meta training), backtester
(exit simulation), live (order placement). T-05 asserts parity on golden paths.

For an entry decided at close t, side in {+1,-1}, filled at P0 = adj_open_{t+1}:

    thr      = m * sigma32_{t,i} * sqrt(h)
    upper    = P0 * (1 + thr)      long: profit-take ; short: stop
    lower    = P0 * (1 - thr)      long: stop        ; short: profit-take
    vertical = exit at adj_open_{t+h+1} via next-open MOO

Touch scan over sessions t+1..t+h using daily H/L; gap-through at a session's
open fills at the OPEN price (conservative, M15-04); both barriers inside one
bar resolve to the STOP side first (tie_break default [IMPL]).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.costs import CostModel


def barrier_exits(entries: pd.DataFrame, adj_open: pd.DataFrame, adj_high: pd.DataFrame,
                  adj_low: pd.DataFrame, adj_close: pd.DataFrame, sigma32: pd.DataFrame,
                  cost_model: CostModel, m: float = 1.5, h: int = 20,
                  tie_break: str = "stop_first",
                  thr_cap: float | None = None) -> pd.DataFrame:
    """entries: DataFrame with columns (date, ticker, side). Returns one row per entry:
    entry_date, ticker, side, fill_date, entry_price, exit_date, exit_price,
    barrier_hit in {upper, lower, vertical, censored, no_fill}, label,
    exit_ret_gross, exit_ret_net, holding_days, day_trade."""
    dates = adj_open.index
    pos = {d: i for i, d in enumerate(dates)}
    cols = {t: j for j, t in enumerate(adj_open.columns)}
    O, H = adj_open.to_numpy(), adj_high.to_numpy()
    L, S = adj_low.to_numpy(), sigma32.to_numpy()
    n_dates = len(dates)
    out = []

    for row in entries.itertuples(index=False):
        t, tick, side = pd.Timestamp(row.date), row.ticker, int(row.side)
        it, j = pos.get(t), cols.get(tick)
        rec = {"entry_date": t, "ticker": tick, "side": side, "fill_date": pd.NaT,
               "entry_price": np.nan, "exit_date": pd.NaT, "exit_price": np.nan,
               "barrier_hit": "no_fill", "label": np.nan, "exit_ret_gross": np.nan,
               "exit_ret_net": np.nan, "holding_days": np.nan, "day_trade": False}
        if it is None or j is None or it + 1 >= n_dates:
            out.append(rec)
            continue
        i0 = it + 1
        P0, sig = O[i0, j], S[it, j]
        if not np.isfinite(P0) or not np.isfinite(sig):
            out.append(rec)
            continue
        thr = m * sig * np.sqrt(h)
        if thr_cap is not None:
            thr = min(thr, thr_cap)   # [IMPL] width cap: high-vol names otherwise
                                      # price barriers they can never touch
        upper, lower = P0 * (1 + thr), P0 * (1 - thr)
        pt_level, stop_level = (upper, lower) if side > 0 else (lower, upper)
        rec.update(fill_date=dates[i0], entry_price=P0)

        exit_price, exit_i, hit = np.nan, None, None
        last_scanned = i0 - 1
        for s in range(i0, min(it + h, n_dates - 1) + 1):     # sessions t+1 .. t+h
            o, hi, lo = O[s, j], H[s, j], L[s, j]
            if not np.isfinite(hi) or not np.isfinite(lo):
                continue                                       # halted bar
            last_scanned = s
            oo = o if np.isfinite(o) else np.nan
            if np.isfinite(oo) and oo >= upper and s > i0:     # gap through at the open
                exit_price, exit_i, hit = oo, s, "upper"
                break
            if np.isfinite(oo) and oo <= lower and s > i0:
                exit_price, exit_i, hit = oo, s, "lower"
                break
            both = hi >= upper and lo <= lower
            if both:
                level = stop_level if tie_break == "stop_first" else pt_level
                exit_price, exit_i = level, s
                hit = "upper" if level == upper else "lower"
                break
            if hi >= upper:
                exit_price, exit_i, hit = upper, s, "upper"
                break
            if lo <= lower:
                exit_price, exit_i, hit = lower, s, "lower"
                break

        if hit is None:                                        # vertical barrier
            iv = it + h + 1
            if iv < n_dates and np.isfinite(O[iv, j]):
                exit_price, exit_i, hit = O[iv, j], iv, "vertical"
            elif last_scanned >= i0:
                exit_price, exit_i, hit = adj_close.iloc[last_scanned, j], last_scanned, "censored"
            else:
                out.append(rec)
                continue

        hold = exit_i - i0
        gross = side * (exit_price / P0 - 1.0)
        net = gross - cost_model.round_trip_frac(side=side, holding_days=hold,
                                                 ticker=tick, date=dates[exit_i])
        if hit == "upper":
            label = 1 if side > 0 else -1
        elif hit == "lower":
            label = -1 if side > 0 else 1
        else:
            label = int(np.sign(gross)) if gross != 0 else 0
        rec.update(exit_date=dates[exit_i], exit_price=exit_price, barrier_hit=hit,
                   label=label, exit_ret_gross=gross, exit_ret_net=net,
                   holding_days=hold, day_trade=(exit_i == i0))
        out.append(rec)

    df = pd.DataFrame(out)
    if len(df):
        df["day_trade"] = df["day_trade"].astype(bool)         # M5-03 -> PDT counter
    return df
