"""T-01 lag law, T-02 purge, T-03 embargo, T-08 shuffled labels, T-09 no same-close fills."""
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.primitives.fwd import forward_return
from src.validation.splits import cpcv, walk_forward
from src.backtest.engine import run_backtest
from src.backtest.costs import CostModel

REPO = Path(__file__).resolve().parent.parent


def test_t01_lag_law():
    idx = pd.date_range('2024-01-01', periods=30, freq='B')
    c = pd.DataFrame({'A': np.arange(30.0) + 100}, index=idx)
    for n in (5, 20):
        f = forward_return(c, n)
        for t in range(0, 30 - 1 - n):
            expected = c.iloc[t + 1 + n, 0] / c.iloc[t + 1, 0] - 1
            assert abs(f.iloc[t, 0] - expected) < 1e-12
    # the deliberately un-lagged variant must differ
    unlagged = c / c.shift(5) - 1
    assert not np.allclose(forward_return(c, 5).dropna().values,
                           unlagged.dropna().values[:len(forward_return(c, 5).dropna())])


def test_t01_shift_containment():
    """`shift(-` may exist only in M3 (primitives) and M5 (labels)."""
    hits = subprocess.run(
        ['grep', '-rl', 'shift(-', str(REPO / 'src')],
        capture_output=True, text=True).stdout.strip().splitlines()
    allowed = {'primitives', 'labels'}
    for h in hits:
        rel = Path(h).relative_to(REPO / 'src')
        assert rel.parts[0] in allowed, f'shift(- leaked into {rel}'


def test_t02_t03_purge_and_embargo():
    dates = pd.date_range('2015-01-01', periods=1500, freq='B')
    span, emb = 61, 21
    splits, _ = cpcv(dates, 6, 2, label_span=span, embargo=emb)
    pos = {d: i for i, d in enumerate(dates)}
    for f in splits:
        test_pos = np.array(sorted(pos[d] for d in f.test))
        # contiguous intervals of the test groups
        breaks = np.where(np.diff(test_pos) > 1)[0]
        ivs = np.split(test_pos, breaks + 1)
        for d in f.train:
            i = pos[d]
            for iv in ivs:
                a, b = iv[0], iv[-1]
                assert not (i + 1 <= b and i + 1 + span >= a), \
                    f'label window of train {i} overlaps test [{a},{b}]'   # T-02
                assert not (b < i <= b + emb), f'train {i} inside embargo after {b}'  # T-03


def test_t08_shuffled_labels(rng):
    import lightgbm as lgb
    n, k = 6000, 15
    X = rng.normal(size=(n, k))
    y = rng.permutation(X[:, 0] * 0.2 + rng.normal(0, 1, n))   # destroyed relation
    d = lgb.Dataset(X[:4000], label=y[:4000])
    bst = lgb.train({'objective': 'regression', 'verbosity': -1, 'seed': 1,
                     'deterministic': True, 'force_col_wise': True},
                    d, num_boost_round=100)
    p = bst.predict(X[4000:])
    ic = np.corrcoef(p, y[4000:])[0, 1]
    assert abs(ic) < 0.05, f'shuffled labels produced IC {ic}'


def test_t09_no_same_close_fills():
    idx = pd.date_range('2024-01-01', periods=10, freq='B')
    op = pd.DataFrame({'A': np.linspace(100, 110, 10)}, index=idx)
    tw = pd.DataFrame({'A': [1.0] * 10}, index=idx)
    res = run_backtest(tw, op, CostModel(0))
    # decision day0 fills day1: day1 return must be 0 (no exposure yet at day0->day1 open move)
    assert res.daily_net.iloc[0] == 0.0
    # exposure exists from day1 open: day2 return equals open-to-open move
    expect = op['A'].iloc[2] / op['A'].iloc[1] - 1
    assert abs(res.daily_net.iloc[1] - expect) < 1e-12
