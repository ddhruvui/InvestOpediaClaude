"""M16 — the two mandatory free baselines (G-08), net of the SAME cost model:
(a) SPY buy-and-hold; (b) plain 12-1 momentum decile L/S on the same universe.
A model that can't beat both does not ship."""
from __future__ import annotations

import pandas as pd

from src.backtest.costs import CostModel
from src.backtest.engine import run_backtest
from src.classical.momentum import momentum_targets


def spy_buy_hold(spy_adj_open: pd.Series, cost_model: CostModel) -> pd.Series:
    """Daily open-to-open SPY total returns, one entry leg cost amortized at start."""
    tw = pd.DataFrame({"SPY": 1.0}, index=spy_adj_open.index)
    res = run_backtest(tw, spy_adj_open.to_frame("SPY"), cost_model)
    return res.daily_net.rename("spy_bh")


def plain_momentum(adj_close: pd.DataFrame, adj_open: pd.DataFrame, mask: pd.DataFrame,
                   cost_model: CostModel, raw_close: pd.DataFrame | None = None) -> pd.Series:
    """The unscaled 12-1 decile long-short baseline (M12.1 plain=True), same engine,
    same costs."""
    w = momentum_targets(adj_close, mask, plain=True, raw_close=raw_close)
    res = run_backtest(w, adj_open, cost_model)
    return res.daily_net.rename("mom_12_1")
