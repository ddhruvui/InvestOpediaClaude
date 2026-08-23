"""T-07 determinism: two identical runs -> bit-identical metric hashes."""
import hashlib
import numpy as np
import pandas as pd


def _run_once(seed=20260717):
    import lightgbm as lgb
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(3000, 10))
    y = X[:, 0] * 0.1 + rng.normal(0, 1, 3000)
    bst = lgb.train({'objective': 'regression', 'verbosity': -1, 'seed': seed,
                     'deterministic': True, 'force_col_wise': True,
                     'num_threads': 1},
                    lgb.Dataset(X[:2000], label=y[:2000]), num_boost_round=60)
    p = bst.predict(X[2000:])
    from src.backtest.engine import run_backtest
    from src.backtest.costs import CostModel
    idx = pd.date_range('2024-01-01', periods=50, freq='B')
    op = pd.DataFrame({'A': 100 + np.cumsum(rng.normal(0, 1, 50))}, index=idx)
    tw = pd.DataFrame({'A': np.clip(np.sign(rng.normal(size=50)), 0, 1)}, index=idx)
    res = run_backtest(tw, op, CostModel(15))
    payload = np.concatenate([p, res.daily_net.values])
    return hashlib.sha256(payload.tobytes()).hexdigest()


def test_t07_bit_identical():
    assert _run_once() == _run_once()
