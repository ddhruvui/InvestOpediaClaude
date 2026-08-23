"""M15 — backtest engine, fast path (§H, §J).

Exact daily accounting with the one-day lag enforced STRUCTURALLY: a target
decided at close t is filled at the open of session t+1; a guard assertion
fails the run if any fill would share the signal's timestamp (T-09). Only
Q-012 open-to-open valuation enters P&L (M15-01); close-to-close appears
nowhere. Costs come from the ONE CostModel (C-08). Dividend liability on
shorts is structurally embedded: valuation uses total-return adjusted prices,
so a short automatically pays the dividend return (the CostModel's explicit
short_div term belongs to the raw-price live path, M18).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from src.backtest.compliance import TaxLots
from src.backtest.costs import CostModel


@dataclass
class BacktestResult:
    daily_net: pd.Series                 # open-to-open net returns, indexed by fill session
    daily_after_tax: pd.Series
    equity: pd.Series
    turnover: pd.Series                  # one-sided, fraction of NAV traded /2
    costs_paid: pd.Series
    fills: pd.DataFrame
    meta: dict = field(default_factory=dict)


def run_backtest(targets: pd.DataFrame, adj_open: pd.DataFrame, cost_model: CostModel,
                 tax_rate: float | None = None, borrow_write_off_sessions: int = 10,
                 nav0: float = 1.0) -> BacktestResult:
    """targets: decision-date-indexed weights (columns must exist in adj_open)."""
    cols = [c for c in targets.columns if c in adj_open.columns]
    missing = set(targets.columns) - set(cols)
    if missing:
        raise KeyError(f"targets reference tickers absent from prices: {sorted(missing)[:5]}")
    dates = adj_open.index
    # Fill session for decision t is the NEXT session — structural lag (G-02).
    tpos = dates.get_indexer(targets.index)
    if (tpos < 0).any():
        raise ValueError("target decision dates must be trading sessions")
    O = adj_open[cols].to_numpy()
    TW = targets[cols].fillna(0.0).to_numpy()
    n_days, n_names = O.shape

    shares = np.zeros(n_names)
    last_px = np.full(n_names, np.nan)
    stale = np.zeros(n_names, dtype=int)
    cash = nav0
    lots = TaxLots(tax_rate)
    nav_series, to_series, cost_series, at_series, fill_rows = {}, {}, {}, {}, []
    decision_for_fill = {p + 1: k for k, p in enumerate(tpos) if p + 1 < n_days}

    prev_nav = nav0
    for i in range(n_days):
        px = O[i].copy()
        have = np.isfinite(px)
        # value book at today's open; stale names carry last price until write-off
        last_px[have] = px[have]
        stale[have] = 0
        stale[~have] += 1
        dead = (~have) & (stale >= borrow_write_off_sessions) & (shares != 0)
        val_px = np.where(have, px, last_px)
        d = dates[i]

        if dead.any():                                   # delisted/halted: liquidate at last print
            for j in np.where(dead)[0]:
                proceeds = shares[j] * last_px[j]
                fee = abs(proceeds) * cost_model.leg_frac()
                cash += proceeds - fee
                if shares[j] > 0:
                    lots.sell(cols[j], shares[j], last_px[j], d)
                shares[j] = 0.0

        pos_val = np.where(np.isfinite(val_px), shares * val_px, 0.0)
        nav = cash + pos_val.sum()

        k = decision_for_fill.get(i)
        traded = 0.0
        costs = 0.0
        if k is not None:
            assert dates[i] > targets.index[k], "T-09: fill may never share the signal timestamp"
            tgt_w = TW[k]
            tgt_dollar = tgt_w * nav
            can_trade = have & np.isfinite(px)
            delta = np.where(can_trade, tgt_dollar - shares * np.where(have, px, 0.0), 0.0)
            notional = np.abs(delta).sum()
            costs = notional * cost_model.leg_frac()
            for j in np.where(np.abs(delta) > 1e-14)[0]:
                q = delta[j] / px[j]
                if q > 0:
                    if shares[j] < 0:                    # short cover (partial or full)
                        lots.sell(cols[j], 0.0, px[j], d)  # shorts: gains tracked via cash flow
                    lots.buy(cols[j], q, px[j], d)
                else:
                    lots.sell(cols[j], min(-q, max(shares[j], 0.0)), px[j], d)
                shares[j] += q
                fill_rows.append((d, cols[j], q, px[j]))
            cash -= delta.sum() + costs
            traded = notional / max(nav, 1e-12)

        # daily borrow on short notional (per-name fee where the table has one)
        short_idx = np.where((shares < 0) & np.isfinite(val_px))[0]
        for j in short_idx:
            fee_bps = cost_model.borrow_fee_bps(cols[j], d)
            borrow = -shares[j] * val_px[j] * fee_bps / 1e4 / 252.0
            cash -= borrow
            costs += borrow

        pos_val = np.where(np.isfinite(val_px), shares * val_px, 0.0)
        nav = cash + pos_val.sum()
        if nav <= 0:                    # ruin: flatten and freeze (no negative-NAV math)
            shares[:] = 0.0
            cash = 0.0
            nav = 0.0
        nav_series[d] = nav
        to_series[d] = traded / 2.0
        cost_series[d] = costs
        ret = nav / prev_nav - 1.0 if abs(prev_nav) > 1e-12 else 0.0
        gain = lots.realized.get(d, 0.0)
        at_series[d] = ret - (lots.rate * max(gain, gain) / max(prev_nav, 1e-12)
                              if lots.rate else 0.0)
        prev_nav = nav

    equity = pd.Series(nav_series).sort_index()
    daily = equity.pct_change().dropna()
    return BacktestResult(
        daily_net=daily,
        daily_after_tax=pd.Series(at_series).sort_index().reindex(daily.index),
        equity=equity,
        turnover=pd.Series(to_series).sort_index(),
        costs_paid=pd.Series(cost_series).sort_index(),
        fills=pd.DataFrame(fill_rows, columns=["date", "ticker", "qty", "price"]),
        meta={"final_nav": float(equity.iloc[-1])},
    )
