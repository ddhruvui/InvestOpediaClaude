"""M17/G-09 — the trials ledger: EVERY configuration ever evaluated (manual runs,
CPCV selections, every Optuna trial finished or pruned) is appended here and
counted in the DSR's N. Small ledger = honest lever; never prune rows."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


class TrialsLedger:
    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path or os.environ.get("LEDGER_PATH", "ledger/trials.parquet"))
        self._ensure_parent()

    def _ensure_parent(self) -> None:
        # Object-store-backed volumes (RunPod S3-FUSE) have no empty directories:
        # mkdir "succeeds" but isdir() stays False until a key exists under the
        # prefix — so materialize it with a .keep file.
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        keep = parent / ".keep"
        if not keep.exists():
            try:
                keep.write_text("")
            except OSError:
                pass

    def _read(self) -> pd.DataFrame:
        if self.path.exists():
            return pd.read_parquet(self.path)
        return pd.DataFrame(columns=["ts_utc", "model", "params_hash", "config_hash",
                                     "objective", "value", "note"])

    def append(self, model: str, params: dict, config_hash: str,
               objective: str, value: float, note: str = "") -> None:
        self._ensure_parent()
        df = self._read()
        row = {"ts_utc": datetime.now(timezone.utc).isoformat(), "model": model,
               "params_hash": hashlib.sha256(
                   json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16],
               "config_hash": config_hash, "objective": objective,
               "value": float(value) if value == value else None, "note": note}
        pd.concat([df, pd.DataFrame([row])], ignore_index=True).to_parquet(self.path, index=False)

    def n_trials(self) -> int:
        """DSR's N (G-09): every evaluated configuration counts."""
        return max(1, len(self._read()))

    def sr_variance(self) -> float | None:
        """Variance of per-period SR across ledger entries whose objective is a Sharpe."""
        df = self._read()
        s = df.loc[df["objective"].str.contains("sharpe", case=False, na=False), "value"].dropna()
        return float(s.var()) if len(s) > 2 else None
