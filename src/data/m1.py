"""M1 landing-layer readers (§4 M1).

The data_acquisition pipeline owns M1 (ingestion + snapshots); this module is
the read side: it loads the Parquet tables build_m1.py writes and presents them
with stable dtypes. Nothing here mutates the landing layer (M1-01).
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import pandas as pd


class M1:
    """Read-only handle on an M1 output directory (local mirror or volume mount)."""

    def __init__(self, m1_dir: str | os.PathLike):
        self.dir = Path(m1_dir)
        if not (self.dir / "sessions.parquet").exists():
            raise FileNotFoundError(f"no M1 layer at {self.dir} (missing sessions.parquet)")

    def _partitioned(self, name: str) -> pd.DataFrame:
        parts = sorted(glob.glob(str(self.dir / name / "part-*.parquet")))
        if not parts:
            return pd.DataFrame()
        return pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)

    def sessions(self) -> pd.DatetimeIndex:
        """Q-001 trading grid (includes future sessions)."""
        s = pd.read_parquet(self.dir / "sessions.parquet")
        return pd.DatetimeIndex(pd.to_datetime(s["date"]).sort_values().unique(), name="date")

    def entities(self) -> pd.DataFrame:
        return pd.read_parquet(self.dir / "entities.parquet")

    def raw_prices(self) -> pd.DataFrame:
        """D-01: date, ticker, open/high/low/close (raw print), volume, close_source,
        adjusted_close_vendor, close_fully_adjusted (Sharadar closeadj where present),
        quarantined, pre_first_price_date."""
        df = self._partitioned("raw_prices_eod")
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def adjustment_factors(self) -> pd.DataFrame:
        """Q-002 vendor-consistent factor = Sharadar closeadj/closeunadj (total-return)."""
        df = self._partitioned("adjustment_factors")
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def corporate_actions(self) -> pd.DataFrame:
        df = pd.read_parquet(self.dir / "corporate_actions.parquet")
        if not df.empty:
            df["date"] = pd.to_datetime(df["date"])
        return df

    def fundamentals_pit(self) -> pd.DataFrame:
        """LONG PIT table; T-11 requires filtering BOTH filing_datetime<=t AND lastupdated<=t."""
        return pd.read_parquet(self.dir / "fundamentals_pit.parquet")

    def estimates_pit(self) -> pd.DataFrame:
        return pd.read_parquet(self.dir / "estimates_pit.parquet")

    def earnings_surprises(self) -> pd.DataFrame:
        return pd.read_parquet(self.dir / "earnings_surprises.parquet")

    def borrow_fees(self) -> pd.DataFrame:
        p = self.dir / "borrow_fees.parquet"
        return pd.read_parquet(p) if p.exists() else pd.DataFrame()

    def manifest(self) -> dict:
        p = self.dir / "_manifest.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def data_snapshot_id(self) -> str:
        return self.manifest().get("data_snapshot_id", "unknown")
