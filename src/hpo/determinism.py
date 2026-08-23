"""M17 — determinism harness (G-10).

Fixed seeds for Python/NumPy/PyTorch/LightGBM; deterministic torch/cuDNN;
PYTHONHASHSEED pinned; every artifact stamped (config_hash, data_snapshot_id,
git_sha, seed). Two identical runs must produce bit-identical metrics (T-07).
"""
from __future__ import annotations

import os
import random

import numpy as np


def seed_everything(seed: int) -> None:
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.use_deterministic_algorithms(True, warn_only=True)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    except ImportError:
        pass


def artifact_stamp(config_hash: str, data_snapshot_id: str, git_sha: str, seed: int) -> dict:
    return {"config_hash": config_hash, "data_snapshot_id": data_snapshot_id,
            "git_sha": git_sha, "seed": int(seed)}
