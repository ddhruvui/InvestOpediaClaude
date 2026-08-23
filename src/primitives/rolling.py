"""M3 — Q-015 rolling-stat engine (C-01).

One cache per panel run. Consumers request by key (column, stat, window); identical
computations are never duplicated (M3-02). Adding a windowed feature = registering a
key here, never writing a new rolling loop (M4-03).
"""
from __future__ import annotations

import pandas as pd

STATS = ("mean", "std", "sum", "max", "min", "ema")
WINDOWS = (5, 10, 14, 20, 26, 30, 60)


class RollingCache:
    def __init__(self, base_columns: dict[str, pd.DataFrame]):
        """base_columns: name -> wide frame, e.g. {'adj_close':..., 'r':..., 'volume':..., 'TR':...}"""
        self._base = dict(base_columns)
        self._cache: dict[tuple[str, str, int], pd.DataFrame] = {}

    def add_base(self, name: str, frame: pd.DataFrame) -> None:
        self._base[name] = frame

    def get(self, column: str, stat: str, window: int) -> pd.DataFrame:
        if stat not in STATS:
            raise KeyError(f"stat {stat!r} not in {STATS}")
        key = (column, stat, int(window))
        if key not in self._cache:
            x = self._base[column]
            mp = max(2, int(window) // 2)
            if stat == "ema":
                out = x.ewm(span=window, adjust=False, min_periods=mp).mean()
            else:
                out = getattr(x.rolling(int(window), min_periods=mp), stat)()
            self._cache[key] = out
        return self._cache[key]
