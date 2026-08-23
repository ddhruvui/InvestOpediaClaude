"""T-16 — continual learning: store round-trip, warm update, refit-mode decision.

Synthetic (date, ticker) feature/label frames; local-safe (no volume, no pods).
"""
import json

import numpy as np
import pandas as pd
import pytest

from src.config import load_config
from src.models.lgbm import LGBMHead, daily_rank_ic
from src.models.store import ModelStore, decide_refit
from src.validation.splits import _purge_embargo


@pytest.fixture(scope="module")
def cfg_hash():
    return load_config()


def _make_xy(rng, n_dates=220, n_names=30, n_feats=8, beta0=0.5):
    dates = pd.date_range("2024-01-01", periods=n_dates, freq="B")
    idx = pd.MultiIndex.from_product(
        [dates, [f"T{i:02d}" for i in range(n_names)]], names=["date", "ticker"])
    X = pd.DataFrame(rng.normal(size=(len(idx), n_feats)).astype(np.float32),
                     index=idx, columns=[f"f{j}" for j in range(n_feats)])
    beta = np.zeros(n_feats)
    beta[0], beta[1] = beta0, -0.3
    y = pd.Series(X.values @ beta + rng.normal(0, 1.0, len(idx)), index=idx)
    return dates, X, y


def _split(dates, X, y, n_valid=60):
    tr_d, va_d = dates[:-n_valid], dates[-n_valid:]
    f = X.index.get_level_values("date")
    tr, va = f.isin(tr_d), f.isin(va_d)
    return X[tr], y[tr], X[va], y[va]


def test_store_round_trip(tmp_path, rng, cfg_hash):
    cfg, config_hash = cfg_hash
    dates, X, y = _make_xy(rng)
    Xtr, ytr, Xva, yva = _split(dates, X, y)
    head = LGBMHead(cfg, 20, 1).fit(Xtr, ytr, Xva, yva, num_boost_round=60)
    ric = head.valid_rank_ic(Xva, yva)
    assert np.isfinite(ric) and ric > 0.05      # planted signal must be found

    store = ModelStore(tmp_path)
    meta = store.save_champion(head, {
        "mode": "full_refit", "parent": None, "train_through": "2024-08-30",
        "full_train_through": "2024-08-30", "valid_rank_ic": ric,
        "config_hash": config_hash, "stamp": {}})
    assert meta["feature_names"] == list(X.columns)

    loaded, lmeta = store.load_champion(cfg, 20, 1)
    # truncated-at-best save must reproduce the validated predictions exactly
    np.testing.assert_allclose(loaded.predict(Xva).values, head.predict(Xva).values,
                               rtol=1e-6)
    assert lmeta["model_file"] == meta["model_file"]


def test_warm_update_learns_and_gates(tmp_path, rng, cfg_hash):
    cfg, config_hash = cfg_hash
    # champion trained on WEAK signal, challenger warm-continued on STRONG data:
    # the update must add trees and win the same-valid-window comparison
    dates_w, Xw, yw = _make_xy(rng, beta0=0.05)
    Xtr, ytr, Xva, yva = _split(dates_w, Xw, yw)
    champ = LGBMHead(cfg, 20, 1).fit(Xtr, ytr, Xva, yva, num_boost_round=40)
    store = ModelStore(tmp_path)
    store.save_champion(champ, {
        "mode": "full_refit", "parent": None, "train_through": "2024-08-30",
        "full_train_through": "2024-08-30",
        "valid_rank_ic": champ.valid_rank_ic(Xva, yva),
        "config_hash": config_hash, "stamp": {}})
    champ_loaded, _ = store.load_champion(cfg, 20, 1)
    n_before = champ_loaded.booster.num_trees()

    dates_s, Xs, ys_ = _make_xy(rng, beta0=0.8)
    Xtr2, ytr2, Xva2, yva2 = _split(dates_s, Xs, ys_)
    chal = LGBMHead(cfg, 20, 1).fit(Xtr2, ytr2, Xva2, yva2,
                                    init_booster=champ_loaded.booster,
                                    num_boost_round=60, learning_rate=0.05)
    assert chal.booster.num_trees() > n_before          # learning continued
    ric_new = chal.valid_rank_ic(Xva2, yva2)
    ric_champ = champ_loaded.valid_rank_ic(Xva2, yva2)  # SAME window comparison
    assert ric_new > ric_champ                          # and it self-improved


def test_decide_refit_modes(tmp_path, rng, cfg_hash):
    cfg, config_hash = cfg_hash
    store = ModelStore(tmp_path)
    sessions = pd.date_range("2024-01-01", periods=700, freq="B")
    horizons = [20]

    mode, why = decide_refit(cfg, store, config_hash, sessions, horizons)
    assert mode == "full" and "champion" in why         # empty store

    dates, X, y = _make_xy(rng)
    Xtr, ytr, Xva, yva = _split(dates, X, y)
    head = LGBMHead(cfg, 20, 1).fit(Xtr, ytr, Xva, yva, num_boost_round=30)
    recent = str(sessions[max(0, sessions.searchsorted(
        pd.Timestamp.today().normalize()) - 3)].date())
    meta = {"mode": "full_refit", "parent": None, "train_through": recent,
            "full_train_through": recent, "valid_rank_ic": 0.03,
            "config_hash": config_hash, "stamp": {}}
    store.save_champion(head, meta)
    mode, _ = decide_refit(cfg, store, config_hash, sessions, horizons)
    assert mode == "update"                             # fresh champion -> warm update

    mode, _ = decide_refit(cfg, store, config_hash, sessions, horizons, forced="full")
    assert mode == "full"                               # REFIT=full wins

    stale = dict(meta, full_train_through=str(sessions[0].date()))
    (tmp_path / "lgbm_h20" / "champion.json").write_text(
        json.dumps(dict(store.champion_meta("lgbm_h20"), **stale)))
    mode, why = decide_refit(cfg, store, config_hash, sessions, horizons)
    assert mode == "full" and "cadence" in why          # past monthly cadence

    mode, _ = decide_refit(cfg, store, "deadbeef", sessions, horizons)
    assert mode == "full"                               # config change -> full


def test_update_window_purged(rng):
    # the warm-update train window must stay clear of the valid year (leak check)
    dates = pd.date_range("2016-01-01", periods=1260, freq="B")
    label_span, n_valid = 61, 252
    n_labeled = len(dates) - label_span
    zone_end = n_labeled - n_valid
    valid_iv = [(zone_end, n_labeled - 1)]
    cand = np.arange(max(0, zone_end - 252), zone_end)
    tr = _purge_embargo(dates, cand, valid_iv, label_span, 0)
    assert len(tr) > 150                                # window survives the purge
    assert tr.max() + 1 + label_span < zone_end + 1 + label_span  # sanity
    assert all(i + 1 + label_span < zone_end or i + 1 > n_labeled - 1 for i in tr)
