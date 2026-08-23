"""M15 two-engine tolerance check [IMPL-17]: the exact-accounting fast path must
agree with the independent closed-form daily-rebalance computation
r_p(d) = sum_i w_i(d-2) * oo_i(d) at zero cost (|dSharpe| <= 0.1 in general;
exact at zero cost with full daily rebalance)."""
import numpy as np
import pandas as pd

from src.backtest.engine import run_backtest
from src.backtest.costs import CostModel


def test_fast_path_matches_closed_form(rng):
    n_days, n_names = 300, 8
    idx = pd.date_range('2022-01-03', periods=n_days, freq='B')
    cols = [f'T{i}' for i in range(n_names)]
    op = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0.0002, 0.015, (n_days, n_names)),
                                             axis=0)), index=idx, columns=cols)
    raw = rng.uniform(0, 1, (n_days, n_names))
    tw = pd.DataFrame(raw / raw.sum(1, keepdims=True), index=idx, columns=cols)

    res = run_backtest(tw, op, CostModel(0))
    oo = op / op.shift(1) - 1
    closed = (tw.shift(2) * oo).sum(axis=1)          # w(t) fills open t+1, earns oo(t+2)
    a = res.daily_net.reindex(idx).fillna(0)
    b = closed.reindex(idx).fillna(0)
    # exact through compounding-free comparison of daily returns
    assert np.nanmax(np.abs(a.values[3:] - b.values[3:])) < 1e-10

    # with costs: the net-vs-gross drag must equal the engine's own costs ledger
    res_c = run_backtest(tw, op, CostModel(15))
    drag = (res.daily_net - res_c.daily_net).sum()
    booked = (res_c.costs_paid / res_c.equity.shift(1).fillna(1.0)).sum()
    assert abs(drag - booked) / booked < 0.05, (drag, booked)
