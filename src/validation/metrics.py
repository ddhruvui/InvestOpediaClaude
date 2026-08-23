"""M16.2 — metric definitions, fixed HERE and used everywhere.

IC_t = Pearson(score, realized fwd label) per day over the universe; RankIC_t =
Spearman; IC/RankIC = time-series means; ICIR = mean/std of the daily series
(qlib convention, unannualized [IMPL]). Net Sharpe = sqrt(252)*mean/std of daily
NET returns; turnover = 0.5*sum|dw|.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def daily_ic(scores: pd.DataFrame, realized: pd.DataFrame,
             mask: pd.DataFrame | None = None, rank: bool = False) -> pd.Series:
    s, y = scores.astype("float64"), realized.astype("float64")
    if mask is not None:
        s, y = s.where(mask), y.where(mask)
    if rank:
        s, y = s.rank(axis=1), y.rank(axis=1)
    sm, ym = s.sub(s.mean(axis=1), axis=0), y.sub(y.mean(axis=1), axis=0)
    cov = (sm * ym).sum(axis=1)
    den = np.sqrt((sm ** 2).sum(axis=1) * (ym ** 2).sum(axis=1))
    return (cov / den.replace(0, np.nan)).rename("ic")


def ic_summary(scores, realized, mask=None) -> dict:
    ic = daily_ic(scores, realized, mask, rank=False).dropna()
    ric = daily_ic(scores, realized, mask, rank=True).dropna()
    return {
        "IC": float(ic.mean()), "ICIR": float(ic.mean() / ic.std()) if len(ic) > 1 else np.nan,
        "RankIC": float(ric.mean()),
        "RankICIR": float(ric.mean() / ric.std()) if len(ric) > 1 else np.nan,
        "n_days": int(len(ic)),
    }


def sharpe(daily_net: pd.Series) -> float:
    r = daily_net.dropna()
    return float(np.sqrt(252) * r.mean() / r.std()) if len(r) > 2 and r.std() > 0 else np.nan


def max_drawdown(daily_net: pd.Series) -> float:
    eq = (1 + daily_net.fillna(0)).cumprod()
    return float((eq / eq.cummax() - 1).min())


def turnover(weights: pd.DataFrame) -> pd.Series:
    return 0.5 * weights.fillna(0).diff().abs().sum(axis=1)


def perf_summary(daily_net: pd.Series, weights: pd.DataFrame | None = None,
                 after_tax: pd.Series | None = None) -> dict:
    out = {
        "sharpe_net": sharpe(daily_net),
        "ann_return": float(daily_net.mean() * 252),
        "ann_vol": float(daily_net.std() * np.sqrt(252)),
        "mdd": max_drawdown(daily_net),
        "n_days": int(daily_net.dropna().shape[0]),
    }
    if after_tax is not None:
        out["sharpe_after_tax"] = sharpe(after_tax)
    if weights is not None:
        to = turnover(weights)
        out["turnover_day"] = float(to.mean())
        out["turnover_month"] = float(to.mean() * 21)
    return out


def alpha_beta(candidate: pd.Series, benchmark: pd.Series) -> dict:
    """OLS of candidate daily net returns on a benchmark's (G-08)."""
    df = pd.concat({"y": candidate, "x": benchmark}, axis=1).dropna()
    if len(df) < 30:
        return {"alpha_ann": np.nan, "beta": np.nan}
    x, y = df["x"].values, df["y"].values
    beta = np.cov(y, x)[0, 1] / np.var(x)
    alpha = y.mean() - beta * x.mean()
    return {"alpha_ann": float(alpha * 252), "beta": float(beta)}
