"""T-04 adjustment, T-06 rank invariance, T-11 PIT fundamentals, T-12 survivorship."""
import numpy as np
import pandas as pd

from src.data.panel import build_panel
from src.data.pit import visible_fundamentals, pit_series
from src.ensemble.rank import ensemble_rank, deciles
from src.backtest.engine import run_backtest
from src.backtest.costs import CostModel


def _mk_prices(idx, ticker, close, opens=None):
    df = pd.DataFrame({
        'date': idx, 'ticker': ticker, 'close': close,
        'open': opens if opens is not None else close,
        'high': np.asarray(close) * 1.01, 'low': np.asarray(close) * 0.99,
        'volume': 1000.0, 'quarantined': False,
    })
    return df


def test_t04_adjustment_split_and_dividend():
    idx = pd.date_range('2024-01-01', periods=8, freq='B')
    # raw: 100 for 4 days, then 2:1 split -> 50, then $1 dividend on day 6 ex-date
    raw = [100, 100, 100, 100, 50, 50, 49, 49]
    prices = _mk_prices(idx, 'A', raw)
    # vendor-consistent factor curve (Sharadar closeadj/closeunadj style):
    # last close fully adjusted = raw; dividend at day6 (1/49), split at day4
    fac = []
    f = 1.0
    for i in range(7, -1, -1):
        fac.append((idx[i], f))
        if i == 6:
            f *= (1 - 1.0 / 50)     # div/raw_close_{exdate-1}, applied to dates BEFORE ex-date
        if i == 4:
            f *= 0.5
    fac_df = pd.DataFrame([{'date': d, 'ticker': 'A', 'factor': v} for d, v in fac])
    actions = pd.DataFrame([
        {'date': idx[4], 'ticker': 'A', 'action_type': 'split', 'value': 2.0,
         'source': 'sharadar_actions'},
        {'date': idx[6], 'ticker': 'A', 'action_type': 'div_cash', 'value': 1.0,
         'source': 'eodhd_div'},
    ])
    panel = build_panel(prices, fac_df, actions, idx)
    ac = panel.adj_close['A']
    r = ac / ac.shift(1) - 1
    # adjusted series continuous at the split (raw halves, adj does not)
    assert abs(r.iloc[4]) < 1e-9, f'split discontinuity in adj series: {r.iloc[4]}'
    # dividend embedded: adj return on ex-date = (49+1)/50-1 = 0 (price fell by the div)
    assert abs(r.iloc[6] - ((49 + 1) / 50 - 1)) < 1e-9
    # raw untouched
    assert panel.raw_close['A'].iloc[4] == 50 and panel.raw_close['A'].iloc[0] == 100
    # volume split-adjusted with the INVERSE factor (pre-split doubled)
    assert abs(panel.adj_volume['A'].iloc[0] - 2000.0) < 1e-9
    assert abs(panel.adj_volume['A'].iloc[5] - 1000.0) < 1e-9


def test_t06_rank_invariance(rng):
    idx = pd.date_range('2024-01-01', periods=30, freq='B')
    cols = [f'T{i}' for i in range(50)]
    mask = pd.DataFrame(True, index=idx, columns=cols)
    s = {f'm{k}': pd.DataFrame(rng.normal(size=(30, 50)), index=idx, columns=cols)
         for k in range(3)}
    e1 = ensemble_rank(s, mask)
    d1 = deciles(e1, mask)
    s2 = dict(s)
    s2['m0'] = np.tanh(s['m0'] * 7) * 3 + 5          # strictly monotone transform
    e2 = ensemble_rank(s2, mask)
    d2 = deciles(e2, mask)
    assert np.allclose(e1.values, e2.values)
    assert d1.equals(d2)


def test_t11_pit_fundamentals():
    idx = pd.date_range('2024-01-01', periods=90, freq='B')
    fund = pd.DataFrame([
        # original filing: Q4 assets=100, filed Jan 25, vendor published same day
        {'ticker': 'A', 'item': 'assets', 'fiscal_period': '2023-12-31',
         'filing_datetime': '2024-01-25', 'lastupdated': '2024-01-25',
         'dimension': 'ARQ', 'value': 100.0},
        # restatement of the SAME quarter published Mar 15
        {'ticker': 'A', 'item': 'assets', 'fiscal_period': '2023-12-31',
         'filing_datetime': '2024-01-25', 'lastupdated': '2024-03-15',
         'dimension': 'ARQ', 'value': 120.0},
    ])
    fv = visible_fundamentals(fund, idx)
    w = pit_series(fv, 'assets', idx, pd.Index(['A']))
    feb = w.loc['2024-02-15', 'A']
    apr = w.loc['2024-04-15', 'A']
    jan_early = w.loc['2024-01-10', 'A']
    assert np.isnan(jan_early), 'value visible before filing'
    assert feb == 100.0, f'restatement leaked backward: {feb}'      # T-11 core
    assert apr == 120.0, 'restatement never became visible'
    # visibility begins the SESSION AFTER filing (M2-03)
    assert np.isnan(w.loc['2024-01-25', 'A']) and w.loc['2024-01-26', 'A'] == 100.0


def test_t12_survivorship_delisting():
    idx = pd.date_range('2024-01-01', periods=30, freq='B')
    live = np.full(30, 100.0)
    op = pd.DataFrame({'DEAD': live, 'B': live}, index=idx)
    op.loc[idx[10]:, 'DEAD'] = np.nan                 # delists after session 9
    op.loc[idx[9], 'DEAD'] = 40.0                     # final crash print
    tw = pd.DataFrame(0.0, index=idx, columns=['DEAD', 'B'])
    tw['DEAD'] = 0.5
    tw['B'] = 0.5
    res = run_backtest(tw, op, CostModel(0), borrow_write_off_sessions=5)
    # the -60% final print is realized (position written off at 40, never silently dropped)
    assert res.equity.iloc[-1] < 0.75, res.equity.iloc[-1]
    assert res.equity.iloc[-1] > 0.60
