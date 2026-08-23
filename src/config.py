"""Configuration registry loader (§3).

One YAML file is the single source of truth; its SHA-256 is the `config_hash`
stamped on every artifact (G-10). Access is attribute-style and read-only so a
module cannot silently mutate a spec value mid-run.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "system.yaml"


class Cfg(Mapping):
    """Read-only nested mapping with attribute access: cfg.barrier.m == cfg['barrier']['m']."""

    def __init__(self, data: dict):
        object.__setattr__(self, "_data", MappingProxyType({
            k: Cfg(v) if isinstance(v, dict) else v for k, v in data.items()
        }))

    def __getattr__(self, k: str) -> Any:
        try:
            return self._data[k]
        except KeyError:
            raise AttributeError(k) from None

    def __setattr__(self, k, v):
        raise TypeError("config is read-only")

    def __getitem__(self, k):
        return self._data[k]

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def to_dict(self) -> dict:
        return {k: v.to_dict() if isinstance(v, Cfg) else v for k, v in self._data.items()}


def load_config(path: str | os.PathLike | None = None) -> tuple[Cfg, str]:
    """Load system.yaml -> (cfg, config_hash). config_hash = SHA-256 of file bytes."""
    p = Path(path or os.environ.get("SYSTEM_CONFIG", DEFAULT_CONFIG_PATH))
    raw = p.read_bytes()
    cfg = Cfg(yaml.safe_load(raw))
    return cfg, hashlib.sha256(raw).hexdigest()


def git_sha() -> str:
    """Current repo commit for artifact stamping (G-10); 'unknown' outside a checkout."""
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, text=True,
                              capture_output=True, check=True).stdout.strip()
    except Exception:
        return "unknown"
