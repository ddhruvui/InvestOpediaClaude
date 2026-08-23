"""T-05 barrier parity/golden paths, T-10 PDT, T-14 cost model, T-15 CPCV combinatorics."""
import numpy as np
import pandas as pd

from src.labels.barriers import barrier_exits
from src.backtest.costs import CostModel
from src.backtest.compliance import PDTCounter
from src.validation.splits import cpcv


def _frames(n=40, price=100.0):
    idx = pd.date_range('2024-01-01', periods=n, freq='B')
    c = pd.DataFrame({'X': [price] * n}, index=idx)
    return idx, c.copy(), c.copy(), c * 1.0005, c * 0.9995


def test_t05_golden_paths():
    cm = CostModel(per_trade_bps=0)
    sig_val, m, h = 0.02, 1.5, 20
    thr = m * sig_val * np.sqrt(h)                     # 13.4164%
    idx, close, op, hi, lo = _frames()
    sig = close * 0 + sig_val

    # (a) clean upper touch on session 4 (intraday, no gap)
    hi.iloc[4] = 100 * (1 + thr) * 1.001
    r = barrier_exits(pd.DataFrame({'date': [idx[0]], 'ticker': ['X'], 'side': [1]}),
                      op, hi, lo, close, sig, cm, m=m, h=h)
    assert r.iloc[0].barrier_hit == 'upper' and r.iloc[0].label == 1
    assert abs(r.iloc[0].exit_price - 100 * (1 + thr)) < 1e-9   # fills AT the barrier

    # (b) day-1 gap-through: open beyond barrier on a later session fills at OPEN
    idx, close, op, hi, lo = _frames()
    op.iloc[3] = hi.iloc[3] = close.iloc[3] = 120.0
    lo.iloc[3] = 119.0
    r = barrier_exits(pd.DataFrame({'date': [idx[0]], 'ticker': ['X'], 'side': [1]}),
                      op, hi, lo, close, close * 0 + sig_val, cm, m=m, h=h)
    assert r.iloc[0].barrier_hit == 'upper' and abs(r.iloc[0].exit_price - 120.0) < 1e-9

    # (c) both barriers inside one bar -> stop side first (tie break)
    idx, close, op, hi, lo = _frames()
    hi.iloc[2] = 100 * (1 + thr) * 1.01
    lo.iloc[2] = 100 * (1 - thr) * 0.99
    r = barrier_exits(pd.DataFrame({'date': [idx[0]], 'ticker': ['X'], 'side': [1]}),
                      op, hi, lo, close, close * 0 + sig_val, cm, m=m, h=h)
    assert r.iloc[0].barrier_hit == 'lower' and r.iloc[0].label == -1

    # (d) vertical: exit at open of t+h+1, holding_days == h
    idx, close, op, hi, lo = _frames()
    r = barrier_exits(pd.DataFrame({'date': [idx[0]], 'ticker': ['X'], 'side': [1]}),
                      op, hi, lo, close, close * 0 + sig_val, cm, m=m, h=h)
    assert r.iloc[0].barrier_hit == 'vertical' and r.iloc[0].holding_days == h

    # (e) same-session exit on the fill day is tagged day_trade (M5-03)
    idx, close, op, hi, lo = _frames()
    hi.iloc[1] = 100 * (1 + thr) * 1.01
    r = barrier_exits(pd.DataFrame({'date': [idx[0]], 'ticker': ['X'], 'side': [1]}),
                      op, hi, lo, close, close * 0 + sig_val, cm, m=m, h=h)
    assert bool(r.iloc[0].day_trade) and r.iloc[0].barrier_hit == 'upper'

    # (f) short side: lower barrier is the profit-take
    idx, close, op, hi, lo = _frames()
    lo.iloc[4] = 100 * (1 - thr) * 0.99
    r = barrier_exits(pd.DataFrame({'date': [idx[0]], 'ticker': ['X'], 'side': [-1]}),
                      op, hi, lo, close, close * 0 + sig_val, cm, m=m, h=h)
    assert r.iloc[0].barrier_hit == 'lower' and r.iloc[0].label == 1
    assert r.iloc[0].exit_ret_gross > 0


def test_t10_pdt_counter():
    days = pd.bdate_range('2024-01-01', periods=10)
    c = PDTCounter(account_equity=10_000)
    for k in range(3):
        assert c.can_day_trade(days[0])
        c.record(days[0])
    assert not c.can_day_trade(days[0])            # 4th blocked
    assert not c.can_day_trade(days[4])            # still inside rolling 5 bdays
    assert c.can_day_trade(days[5])                # window rolled off
    assert PDTCounter(account_equity=50_000).can_day_trade(days[0])  # >=25k unenforced


def test_t14_cost_model():
    cm = CostModel(per_trade_bps=15, slippage_bps=5, borrow_gc_bps_yr=50)
    assert abs(cm.leg_frac() - 20e-4) < 1e-12
    rt_long = cm.round_trip_frac(side=1, holding_days=10)
    assert abs(rt_long - 40e-4) < 1e-12
    rt_short = cm.round_trip_frac(side=-1, holding_days=10)
    assert abs(rt_short - (40e-4 + 50 / 1e4 / 252 * 10)) < 1e-12
    tbl = pd.DataFrame({'date': [pd.Timestamp('2024-01-02')], 'ticker': ['HTB'],
                        'fee_bps_yr': [800.0]})
    cm2 = CostModel(borrow_table=tbl)
    assert cm2.borrow_fee_bps('HTB', '2024-06-01') == 800.0
    assert cm2.borrow_fee_bps('AAPL', '2024-06-01') == 50.0


def test_t15_cpcv():
    dates = pd.date_range('2010-01-01', periods=1260, freq='B')
    splits, paths = cpcv(dates, 6, 2)
    assert len(splits) == 15
    from collections import Counter
    appear = Counter()
    for f in splits:
        for g in eval(f.tag.split(':')[1]):
            appear[g] += 1
    assert all(appear[g] == 5 for g in range(6))
    assert len(paths) == 5 and all(sorted(set(p)) == sorted(set(p)) and len(p) == 6 for p in paths)
    # every path draws each group's slice from a split where that group WAS in test
    for p in paths:
        for g, ci in enumerate(p):
            assert g in eval(splits[ci].tag.split(':')[1])
