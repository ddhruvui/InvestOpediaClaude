"""M17 — Optuna HPO (§J). Objective = purged-CV Rank IC or net Sharpe; search
spaces confined to spec ranges (M17-01); budget <= 100 trials/model (M17-02);
every trial (finished or pruned) -> trials ledger -> DSR's N (G-09); test folds
and the hold-out are unreachable from the objective by construction (M17-04)."""
from __future__ import annotations

from typing import Callable

import optuna

from src.hpo.ledger import TrialsLedger

SPACES = {
    "lgbm": lambda t: {
        "learning_rate": t.suggest_float("learning_rate", 0.05, 0.2),
        "num_leaves": t.suggest_int("num_leaves", 100, 300),
    },
    "barrier": lambda t: {
        "m": t.suggest_float("m", 1.0, 2.0),
        "h_days": t.suggest_categorical("h_days", [10, 15, 20, 30]),
    },
    "meta": lambda t: {
        "p_threshold": t.suggest_float("p_threshold", 0.50, 0.65),
    },
    "gru": lambda t: {
        "lr_start": t.suggest_float("lr_start", 3e-4, 3e-3, log=True),
        "dropout": t.suggest_float("dropout", 0.1, 0.3),
    },
}


def run_search(model: str, objective_fn: Callable[[dict], float], config_hash: str,
               objective_name: str = "rank_ic", n_trials: int = 100,
               seed: int = 20260717, ledger: TrialsLedger | None = None) -> optuna.Study:
    """objective_fn(params) must evaluate on PURGED train/valid folds only."""
    assert n_trials <= 100, "M17-02: budget <= 100 trials per model"
    ledger = ledger or TrialsLedger()
    space = SPACES[model]

    def obj(trial: optuna.Trial) -> float:
        params = space(trial)
        try:
            value = objective_fn(params)
        except optuna.TrialPruned:
            ledger.append(model, params, config_hash, objective_name, float("nan"),
                          note="pruned")
            raise
        ledger.append(model, params, config_hash, objective_name, value, note="optuna")
        return value

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=seed))
    study.optimize(obj, n_trials=n_trials)
    return study
