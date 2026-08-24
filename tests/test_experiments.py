"""M17 experiment harness: thr_cap semantics, causal member weights, and
equal-weight parity with the M10 ensemble."""
import numpy as np
import pandas as pd

from src.backtest.costs import CostModel
from src.ensemble.rank import ensemble_rank, member_ranks
from src.labels.barriers import barrier_exits
from src.pipeline.experiments import (daily_rank_ic, trailing_ic_weights,
                                      weighted_ensemble)


def _frames(n=40, price=100.0):
    idx = pd.date_range('2024-01-01', periods=n, freq='B')
    c = pd.DataFrame({'X': [price] * n}, index=idx)
    return idx, c.copy(), c.copy(), c * 1.0005, c * 0.9995


def test_thr_cap_binds_only_when_smaller():
    cm = CostModel(per_trade_bps=0)
    m, h = 1.5, 20
    sig_val = 0.10                                   # thr = 67% uncapped
    idx, close, op, hi, lo = _frames()
    sig = close * 0 + sig_val
    hi.iloc[4] = 100 * 1.30                          # +30% spike on session 4

    ent = pd.DataFrame({'date': [idx[0]], 'ticker': ['X'], 'side': [1]})
    r_un = barrier_exits(ent, op, hi, lo, close, sig, cm, m=m, h=h)
    assert r_un.iloc[0].barrier_hit == 'vertical'    # ±67% barrier never touched

    r_cap = barrier_exits(ent, op, hi, lo, close, sig, cm, m=m, h=h, thr_cap=0.25)
    assert r_cap.iloc[0].barrier_hit == 'upper'      # capped at ±25% -> touched
    assert abs(r_cap.iloc[0].exit_price - 100 * 1.25) < 1e-9

    # cap larger than thr is a no-op
    sig2 = close * 0 + 0.02                          # thr = 13.4%
    r_noop = barrier_exits(ent, op, hi, lo, close, sig2, cm, m=m, h=h, thr_cap=0.50)
    r_base = barrier_exits(ent, op, hi, lo, close, sig2, cm, m=m, h=h)
    assert r_noop.iloc[0].barrier_hit == r_base.iloc[0].barrier_hit


def _rank_frames(rng, n=400, k=30):
    idx = pd.date_range('2022-01-03', periods=n, freq='B')
    cols = [f'T{i:02d}' for i in range(k)]
    a = pd.DataFrame(rng.random((n, k)), index=idx, columns=cols).rank(axis=1, pct=True)
    b = pd.DataFrame(rng.random((n, k)), index=idx, columns=cols).rank(axis=1, pct=True)
    fwd = pd.DataFrame(rng.normal(0, .02, (n, k)), index=idx, columns=cols)
    return idx, {'A': a, 'B': b}, fwd


def test_trailing_ic_weights_are_causal(rng):
    idx, ranks, fwd = _rank_frames(rng)
    w1 = trailing_ic_weights(ranks, fwd)
    cut = 300
    fwd2 = fwd.copy()
    fwd2.iloc[cut:] = rng.normal(0, .02, fwd2.iloc[cut:].shape)   # scramble the future
    w2 = trailing_ic_weights(ranks, fwd2)
    lag = 22
    pd.testing.assert_frame_equal(w1.iloc[:cut + lag], w2.iloc[:cut + lag])
    # weights sum to 1 wherever defined
    s = w1.sum(axis=1).dropna()
    assert np.allclose(s, 1.0)


def test_weighted_ensemble_equal_matches_m10(rng):
    idx, ranks, _ = _rank_frames(rng)
    mask = pd.DataFrame(True, index=idx, columns=ranks['A'].columns)
    ours = weighted_ensemble(member_ranks(ranks, mask), mask)
    theirs = ensemble_rank(ranks, mask)
    pd.testing.assert_frame_equal(ours, theirs, check_exact=False, atol=1e-12)


def test_daily_rank_ic_sign(rng):
    idx, ranks, _ = _rank_frames(rng)
    fwd = ranks['A'] + rng.normal(0, .01, ranks['A'].shape)   # A predicts fwd
    ic = daily_rank_ic(ranks['A'], fwd)
    assert ic.mean() > 0.9
