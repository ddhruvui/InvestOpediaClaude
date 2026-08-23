"""M1/M3 — D-09 index (SPY) loader from the raw EODHD JSON on the volume.

EODHD `adjusted_close` for SPY embeds distributions -> total-return close (Q-019).
adj_open = open * adjusted_close/close (same-row ratio).
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def load_spy(eod_dir: str | Path, symbol: str = "SPY") -> pd.DataFrame:
    base = Path(eod_dir) / "market"
    for name in (f"{symbol}.json", f"{symbol}.US.json"):   # fetcher stores vendor code
        p = base / name
        if p.exists():
            break
    else:
        raise FileNotFoundError(f"no {symbol}[.US].json under {base}")
    rows = json.loads(p.read_text())
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    out = pd.DataFrame(index=df.index)
    ratio = df["adjusted_close"] / df["close"]
    out["adj_close"] = df["adjusted_close"]
    out["adj_open"] = df["open"] * ratio
    out["raw_close"] = df["close"]
    return out
