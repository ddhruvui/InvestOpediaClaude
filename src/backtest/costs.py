"""C-08 — THE cost model (G-07/G-15): one implementation serving backtests,
baselines, meta-labeling outcomes, and live estimation.

  cost_leg   = notional x (per_trade_bps + slippage_bps)/1e4
  borrow_day = short_notional x fee_bps_yr/1e4/252     (per-name table, GC default 30)
  short_div  = dividend liability on short positions
"""
from __future__ import annotations

import numpy as np
import pandas as pd


class CostModel:
    def __init__(self, per_trade_bps: float = 15.0, slippage_bps: float = 0.0,
                 borrow_gc_bps_yr: float = 30.0,
                 borrow_table: pd.DataFrame | None = None):
        """borrow_table: optional long df (date, ticker, fee_bps_yr) — M1 `borrow_fees`.
        NULL fees (vendor had no quote that day) are dropped, never read as a rate."""
        self.per_trade_bps = float(per_trade_bps)
        self.slippage_bps = float(slippage_bps)
        self.borrow_gc_bps_yr = float(borrow_gc_bps_yr)
        self._borrow: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        if borrow_table is not None and len(borrow_table):
            b = borrow_table[["ticker", "date", "fee_bps_yr"]].dropna(subset=["fee_bps_yr"])
            b = b.assign(date=pd.to_datetime(b["date"])).sort_values(["ticker", "date"])
            for t, g in b.groupby("ticker", sort=False):
                self._borrow[t] = (g["date"].to_numpy("datetime64[ns]"),
                                   g["fee_bps_yr"].to_numpy(float))

    # ---- per-leg / per-trip fractions -------------------------------------
    def leg_frac(self) -> float:
        return (self.per_trade_bps + self.slippage_bps) / 1e4

    def borrow_fee_bps(self, ticker: str | None = None, date=None) -> float:
        """Layered: the name's last quote on/before `date`; before its first quote, that
        first quote (history is ~1y rolling, so the past borrows it); never quoted -> GC."""
        quotes = self._borrow.get(ticker) if date is not None else None
        if quotes is None:
            return self.borrow_gc_bps_yr
        dates, fees = quotes
        i = np.searchsorted(dates, np.datetime64(pd.Timestamp(date), "ns"), side="right") - 1
        return float(fees[max(i, 0)])

    def round_trip_frac(self, side: int = 1, holding_days: int = 0,
                        ticker: str | None = None, date=None,
                        short_div_frac: float = 0.0) -> float:
        """Total round-trip cost as a fraction of entry notional (M5-02 net labels)."""
        c = 2.0 * self.leg_frac()
        if side < 0:
            c += self.borrow_fee_bps(ticker, date) / 1e4 / 252.0 * max(0, holding_days)
            c += short_div_frac
        return c
