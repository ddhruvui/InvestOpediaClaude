"""M6 — LightGBM horizon heads (L4, primary model; BP3).

Three independent boosters, one per horizon head (M6-01) — never one model
stretched across the range. Hyperparameters are the qlib-tuned config (M6-02);
determinism per G-10 (M6-04); early stopping monitors the PURGED validation
segment only, with Rank IC logged alongside (M6-03); top-20 gain importances
are the M7 input contract (M6-06).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import lightgbm as lgb

from scipy import stats


def lgbm_params(cfg, seed: int) -> dict:
    return {
        "objective": "regression" if cfg.lgbm.objective == "mse" else "lambdarank",
        "learning_rate": cfg.lgbm.learning_rate,
        "num_leaves": cfg.lgbm.num_leaves,
        "max_depth": cfg.lgbm.max_depth,
        "colsample_bytree": cfg.lgbm.colsample_bytree,
        "subsample": cfg.lgbm.subsample,
        "bagging_freq": 1,                      # required for subsample to engage
        "lambda_l1": cfg.lgbm.lambda_l1,
        "lambda_l2": cfg.lgbm.lambda_l2,
        "deterministic": bool(cfg.lgbm.deterministic),
        "force_col_wise": True,
        "seed": int(seed),
        "verbosity": -1,
        "metric": "l2",
    }


def _rank_ic_feval(valid_dates: np.ndarray):
    """Custom eval: mean daily Spearman between preds and label (logged, not the
    early-stop metric — M6-03 stops on validation loss)."""
    def feval(preds: np.ndarray, data: lgb.Dataset):
        y = data.get_label()
        df = pd.DataFrame({"d": valid_dates, "p": preds, "y": y})
        ics = df.groupby("d").apply(
            lambda g: stats.spearmanr(g["p"], g["y"])[0] if len(g) > 2 else np.nan,
            include_groups=False).to_numpy(dtype=float)
        val = float(np.nanmean(ics)) if np.isfinite(ics).any() else 0.0
        return "rank_ic", val, True
    return feval


class LGBMHead:
    def __init__(self, cfg, horizon: int, seed: int):
        self.cfg, self.horizon, self.seed = cfg, horizon, seed
        self.params = lgbm_params(cfg, seed)
        self.booster: lgb.Booster | None = None
        self.feature_names: list[str] = []
        self.evals: dict = {}

    def fit(self, X_tr: pd.DataFrame, y_tr: pd.Series,
            X_va: pd.DataFrame, y_va: pd.Series) -> "LGBMHead":
        self.feature_names = list(X_tr.columns)
        dtr = lgb.Dataset(X_tr.values, label=y_tr.values,
                          feature_name=self.feature_names, free_raw_data=True)
        dva = dtr.create_valid(X_va.values, label=y_va.values)
        valid_dates = X_va.index.get_level_values("date").values
        rec: dict = {}
        self.booster = lgb.train(
            self.params, dtr, num_boost_round=int(self.cfg.lgbm.num_boost_round),
            valid_sets=[dva], valid_names=["valid"],
            feval=_rank_ic_feval(valid_dates),
            callbacks=[lgb.early_stopping(int(self.cfg.lgbm.early_stopping_rounds),
                                          first_metric_only=True, verbose=False),
                       lgb.record_evaluation(rec)])
        self.evals = rec
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        assert self.booster is not None
        return pd.Series(self.booster.predict(X.values,
                                              num_iteration=self.booster.best_iteration),
                         index=X.index, name=f"lgbm_h{self.horizon}")

    def top_importance(self, k: int = 20) -> list[str]:
        """M6-06: top-k features by GAIN importance — the M7 input contract."""
        assert self.booster is not None
        imp = pd.Series(self.booster.feature_importance("gain"), index=self.feature_names)
        return list(imp.sort_values(ascending=False).head(k).index)
