"""M11 — meta-labeling gate (§G, BP12).

Secondary small-LightGBM classifier on TRIPLE-BARRIER outcomes of the primary
signal's candidate trades: label = 1[exit_ret_NET > 0] (M5-02). The primary
model supplies the side; the meta model supplies P(profit | side, context) and
a size multiplier prop to (p - 0.5). Trains strictly after, and never
overlapping, the primary model's data per fold (M11-04); purged like everything.

Adoption gate (M11-02): ships only if it cuts turnover materially at
equal-or-better net Sharpe vs the ungated book on identical folds.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import lightgbm as lgb

from src.backtest.costs import CostModel
from src.labels.barriers import barrier_exits

META_FEATURES = ["side", "ensemble_rank", "sigma32", "vol_20", "regime",
                 "days_to_earnings", "days_since_earnings", "mom_12_1", "size"]


def candidates_from_deciles(dec: pd.DataFrame, dates: pd.DatetimeIndex,
                            side: int = 1) -> pd.DataFrame:
    """Daily decile-10 selections -> (date, ticker, side) candidate entries."""
    rows = []
    sub = dec.loc[dec.index.isin(dates)]
    for d, row in sub.iterrows():
        for t in row.index[row == 10]:
            rows.append((d, t, side))
    return pd.DataFrame(rows, columns=["date", "ticker", "side"])


def meta_context(entries: pd.DataFrame, ens_rank: pd.DataFrame, sigma32: pd.DataFrame,
                 vol20: pd.DataFrame, mom: pd.DataFrame, size_f: pd.DataFrame | None,
                 regime: pd.Series | None, days_to: pd.DataFrame | None,
                 days_since: pd.DataFrame | None) -> pd.DataFrame:
    """Context features at the ENTRY DECISION close (M11 [IMPL list])."""
    X = pd.DataFrame(index=entries.index)
    idx = list(zip(entries["date"], entries["ticker"]))

    def pick(frame):
        if frame is None:
            return np.nan
        s = frame.stack(future_stack=True)
        return s.reindex(idx).to_numpy()

    X["side"] = entries["side"].to_numpy()
    X["ensemble_rank"] = pick(ens_rank)
    X["sigma32"] = pick(sigma32)
    X["vol_20"] = pick(vol20)
    X["regime"] = (regime.reindex(entries["date"]).to_numpy()
                   if regime is not None else 1.0)
    X["days_to_earnings"] = pick(days_to)
    X["days_since_earnings"] = pick(days_since)
    X["mom_12_1"] = pick(mom)
    X["size"] = pick(size_f)
    return X


def train_meta(X: pd.DataFrame, y: pd.Series, cfg, seed: int) -> lgb.Booster:
    params = {"objective": "binary", "num_leaves": int(cfg.meta.num_leaves),
              "learning_rate": float(cfg.meta.learning_rate), "verbosity": -1,
              "deterministic": True, "force_col_wise": True, "seed": int(seed),
              "metric": "binary_logloss"}
    n = len(X)
    cut = int(n * 0.8)
    dtr = lgb.Dataset(X.iloc[:cut].values, label=y.iloc[:cut].values,
                      feature_name=list(X.columns))
    dva = dtr.create_valid(X.iloc[cut:].values, label=y.iloc[cut:].values)
    return lgb.train(params, dtr, num_boost_round=int(cfg.meta.num_boost_round),
                     valid_sets=[dva],
                     callbacks=[lgb.early_stopping(int(cfg.meta.early_stopping_rounds),
                                                   verbose=False)])


def meta_outcomes(entries: pd.DataFrame, panel, sigma32: pd.DataFrame,
                  cost_model: CostModel, cfg) -> pd.DataFrame:
    """Barrier outcomes for candidate entries via the ONE C-06 engine."""
    res = barrier_exits(entries, panel.adj_open, panel.adj_high, panel.adj_low,
                        panel.adj_close, sigma32, cost_model,
                        m=float(cfg.barrier.m), h=int(cfg.barrier.h_days),
                        tie_break=str(cfg.barrier.tie_break))
    res["y"] = (res["exit_ret_net"] > 0).astype(float)
    return res


def meta_multiplier(p: pd.Series, threshold: float = 0.55,
                    sizing: str = "prop_p_minus_half") -> pd.Series:
    """Gate (p > threshold) x size mult prop (p - 0.5), normalized to mean 1 over
    the gated set (M11-01: gross re-matched by M14's budget)."""
    gate = p > threshold
    mult = (p - 0.5).clip(lower=0.0) if sizing == "prop_p_minus_half" \
        else pd.Series(1.0, index=p.index)
    mult = mult.where(gate, 0.0)
    m = mult[mult > 0]
    return mult / m.mean() if len(m) else mult
