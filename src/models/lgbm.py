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


def daily_rank_ic(pred: pd.Series, y: pd.Series) -> float:
    """Mean daily Spearman between two (date, ticker)-indexed series. Used for the
    champion-vs-challenger comparison, where both models must be scored on the SAME
    validation window rather than each on the window it was trained against."""
    df = pd.DataFrame({"p": pred, "y": y}).dropna()
    if df.empty:
        return float("nan")
    d = df.index.get_level_values("date")
    ics = df.groupby(d).apply(
        lambda g: stats.spearmanr(g["p"], g["y"])[0] if len(g) > 2 else np.nan,
        include_groups=False).to_numpy(dtype=float)
    return float(np.nanmean(ics)) if np.isfinite(ics).any() else float("nan")


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
            X_va: pd.DataFrame, y_va: pd.Series,
            init_booster: "lgb.Booster | None" = None,
            num_boost_round: int | None = None,
            learning_rate: float | None = None) -> "LGBMHead":
        """init_booster continues training on top of an existing model (continual
        learning): new trees fit the residuals of the old ones. With init_model the
        recorded evals cover only the NEW rounds while booster.best_iteration counts
        from the init model's trees — never index evals by best_iteration; use
        valid_rank_ic() for the honest number in either mode."""
        self.feature_names = list(X_tr.columns)
        params = dict(self.params)
        if learning_rate is not None:
            params["learning_rate"] = float(learning_rate)
        dtr = lgb.Dataset(X_tr.values, label=y_tr.values,
                          feature_name=self.feature_names, free_raw_data=True)
        dva = dtr.create_valid(X_va.values, label=y_va.values)
        valid_dates = X_va.index.get_level_values("date").values
        rec: dict = {}
        self.booster = lgb.train(
            params, dtr,
            num_boost_round=int(num_boost_round or self.cfg.lgbm.num_boost_round),
            init_model=init_booster,
            valid_sets=[dva], valid_names=["valid"],
            feval=_rank_ic_feval(valid_dates),
            callbacks=[lgb.early_stopping(int(self.cfg.lgbm.early_stopping_rounds),
                                          first_metric_only=True, verbose=False),
                       lgb.record_evaluation(rec)])
        self.evals = rec
        return self

    @classmethod
    def from_booster(cls, cfg, horizon: int, seed: int, booster: lgb.Booster,
                     feature_names: list[str]) -> "LGBMHead":
        """Wrap a stored booster for scoring / continued training (no fit here)."""
        head = cls(cfg, horizon, seed)
        head.booster = booster
        head.feature_names = list(feature_names)
        return head

    def valid_rank_ic(self, X_va: pd.DataFrame, y_va: pd.Series) -> float:
        """Mean daily Spearman on a validation window — safe in both fresh-fit and
        continued-training modes (see fit docstring)."""
        return daily_rank_ic(self.predict(X_va), y_va)

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
