"""M16.2 — Deflated Sharpe Ratio (Bailey & Lopez de Prado, canonical form).

Conventions PINNED by the v1.0.1 audit (§16.2): SR, SR*, and every trials-ledger
entry are PER-PERIOD (daily, unannualized); T = number of daily observations;
gamma4 is RAW kurtosis (normal = 3). N = trials-ledger count (G-09).

    DSR = Phi( (SR - SR*) * sqrt(T-1) / sqrt(1 - g3*SR + ((g4-1)/4)*SR^2) )
    SR* = sqrt(V[SR_trials]) * ((1-gamma)*Phi^-1(1-1/N) + gamma*Phi^-1(1-1/(N*e)))

Calibration note: deliberately harsh — a true ann. Sharpe 1.5 over ~5y deflated
for 100 trials scores ~0.82. Long windows and a SMALL trials ledger are the only
honest levers.
"""
from __future__ import annotations

import numpy as np
from scipy import stats

EULER_GAMMA = 0.5772156649015329


def expected_max_sharpe(sr_trials_var: float, n_trials: int) -> float:
    """SR* — expected max of N iid trials with variance V[SR_trials]."""
    if n_trials <= 1 or sr_trials_var <= 0:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(sr_trials_var) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe(daily_returns, n_trials: int, sr_trials_var: float | None = None) -> dict:
    """daily_returns: the candidate's daily NET return series (unannualized SR inside)."""
    r = np.asarray(daily_returns, dtype=float)
    r = r[np.isfinite(r)]
    T = len(r)
    if T < 30 or r.std() == 0:
        return {"DSR": np.nan, "SR_daily": np.nan, "SR_star": np.nan, "T": T, "N": n_trials}
    sr = r.mean() / r.std(ddof=1)
    g3 = stats.skew(r)
    g4 = stats.kurtosis(r, fisher=False)          # RAW kurtosis, normal = 3
    if sr_trials_var is None:
        sr_trials_var = np.var([sr])              # degenerate single-trial fallback
    sr_star = expected_max_sharpe(sr_trials_var, n_trials)
    denom = np.sqrt(max(1e-12, 1 - g3 * sr + ((g4 - 1) / 4.0) * sr ** 2))
    z = (sr - sr_star) * np.sqrt(T - 1) / denom
    return {"DSR": float(stats.norm.cdf(z)), "SR_daily": float(sr),
            "SR_star": float(sr_star), "T": T, "N": int(n_trials),
            "skew": float(g3), "kurtosis_raw": float(g4)}
