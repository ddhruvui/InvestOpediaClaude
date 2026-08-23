"""M7 — GRU horizon heads (BP8).

Architecture fixed by spec: GRU hidden 64, 2 layers, dropout 0.2, linear head ->
scalar score; Adam 1e-3 -> 1e-4 (/10 on valid-RankIC plateau, patience 3 [IMPL]);
lookback 60 sessions; inputs = top-20 LGBM-gain features per fold/head (M6-06
contract). Same labels as M6 (M7-02); MSE on the CS-demeaned label; batches are
WHOLE-DATE groups so each batch is cross-sectionally coherent (M7-03); early stop
on valid RankIC patience 5; 3-5-seed ensemble averaged (M7-04). Determinism G-10.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

from scipy import stats


class GRUHead(nn.Module):
    def __init__(self, n_features: int = 20, hidden: int = 64, layers: int = 2,
                 dropout: float = 0.2):
        super().__init__()
        self.gru = nn.GRU(n_features, hidden, num_layers=layers, dropout=dropout,
                          batch_first=True)
        self.head = nn.Linear(hidden, 1)

    def forward(self, x):                          # x: (B, T, F)
        out, _ = self.gru(x)
        return self.head(out[:, -1, :]).squeeze(-1)


class SequenceStore:
    """Lazy per-date sequence server over one shared (T, N, F) float32 array —
    building every date's tensor up front would cost ~lookback x the memory."""

    def __init__(self, feats: pd.DataFrame, cols: list[str], lookback: int = 60):
        wide = {c: feats[c].unstack("ticker") for c in cols}
        ref = next(iter(wide.values()))
        self.dates, self.tickers = ref.index, ref.columns
        self.lookback = lookback
        self.arr = np.stack([w.reindex(index=self.dates, columns=self.tickers)
                             .to_numpy(dtype=np.float32) for w in wide.values()],
                            axis=-1)                        # (T, N, F)

    def get(self, d) -> tuple[np.ndarray, list] | None:
        i = self.dates.searchsorted(d)
        if i >= len(self.dates) or self.dates[i] != d or i + 1 < self.lookback:
            return None
        block = self.arr[i - self.lookback + 1: i + 1]       # (L, N, F) view
        alive = ~np.isnan(block[-1]).all(axis=-1)
        if not alive.any():
            return None
        X = np.nan_to_num(block[:, alive, :]).transpose(1, 0, 2)
        return X, list(self.tickers[alive])


def make_sequences(store: SequenceStore, dates: pd.DatetimeIndex) -> dict:
    """Materialize {date: (X, names)} for a (small) date set, e.g. validation."""
    out = {}
    for d in dates:
        got = store.get(d)
        if got:
            out[d] = got
    return out


def _rank_ic(preds: dict, labels: pd.DataFrame) -> float:
    ics = []
    for d, (p, names) in preds.items():
        if d not in labels.index:
            continue
        y = labels.loc[d, names].to_numpy(dtype=float)
        ok = np.isfinite(y) & np.isfinite(p)
        if ok.sum() > 2:
            ics.append(stats.spearmanr(p[ok], y[ok])[0])
    return float(np.nanmean(ics)) if ics else np.nan


def train_gru(seqs_train: dict, seqs_valid: dict, label_wide: pd.DataFrame,
              n_features: int, cfg, seed: int, device: str | None = None,
              max_epochs: int = 100) -> tuple[list[GRUHead], dict]:
    """Trains the seed ensemble; returns (models, info). Each seq dict: date -> (X, names)."""
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    models, info = [], {"seeds": []}
    n_seeds = int(cfg.gru.seed_ensemble)
    for k in range(n_seeds):
        torch.manual_seed(seed + k)
        model = GRUHead(n_features, int(cfg.gru.hidden_size), int(cfg.gru.num_layers),
                        float(cfg.gru.dropout)).to(device)
        opt = torch.optim.Adam(model.parameters(), lr=float(cfg.gru.lr_start))
        sched = torch.optim.lr_scheduler.ReduceLROnPlateau(
            opt, mode="max", factor=0.1, patience=int(cfg.gru.lr_plateau_patience))
        best_ric, best_state, patience = -np.inf, None, 0
        train_days = list(seqs_train)
        rng = np.random.default_rng(seed + k)
        for epoch in range(max_epochs):
            model.train()
            rng.shuffle(train_days)
            for d in train_days:                   # whole-date batches (M7-03)
                got = seqs_train[d] if isinstance(seqs_train, dict) else None
                if got is None:
                    continue
                X, names = got
                y = label_wide.loc[d].reindex(names).to_numpy(dtype=np.float32) \
                    if d in label_wide.index else None
                if y is None:
                    continue
                ok = np.isfinite(y)
                if ok.sum() < 3:
                    continue
                xb = torch.from_numpy(X[ok]).to(device)
                yb = torch.from_numpy(y[ok]).to(device)
                opt.zero_grad()
                loss = nn.functional.mse_loss(model(xb), yb)
                loss.backward()
                opt.step()
            model.eval()
            with torch.no_grad():
                preds = {d: (model(torch.from_numpy(X).to(device)).cpu().numpy(), names)
                         for d, (X, names) in seqs_valid.items()}
            ric = _rank_ic(preds, label_wide)
            sched.step(ric if np.isfinite(ric) else 0.0)
            if np.isfinite(ric) and ric > best_ric:
                best_ric, patience = ric, 0
                best_state = {k2: v.cpu().clone() for k2, v in model.state_dict().items()}
            else:
                patience += 1
                if patience >= int(cfg.gru.early_stop_patience):
                    break
            if opt.param_groups[0]["lr"] < float(cfg.gru.lr_end) * 0.99:
                break
        if best_state:
            model.load_state_dict(best_state)
        model.eval()
        models.append(model)
        info["seeds"].append({"seed": seed + k, "best_valid_ric": float(best_ric),
                              "epochs": epoch + 1})
    return models, info


def predict_gru(models: list[GRUHead], seqs: dict, tickers: pd.Index,
                device: str | None = None) -> pd.DataFrame:
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    rows = {}
    with torch.no_grad():
        for d, (X, names) in seqs.items():
            xb = torch.from_numpy(X).to(device)
            p = np.mean([m(xb).cpu().numpy() for m in models], axis=0)
            rows[d] = pd.Series(p, index=names)
    return pd.DataFrame(rows).T.reindex(columns=tickers)
