"""M15/M18 — PDT counter & tax lots (§H, §I; G-14).

PDT: same-session open+close pairs (incl. same-day short covers) increment a
rolling 5-BUSINESS-DAY counter; under $25k equity the 4th is disallowed ->
`defer_to_next_open` [IMPL]. The flat <=3 budget deliberately ignores FINRA's
6%-of-total-trades carve-out (strictly conservative [IMPL simplification]);
shorting requires margin, so a cash account is NOT a workaround.
"""
from __future__ import annotations

from collections import deque

import pandas as pd


class PDTCounter:
    def __init__(self, account_equity: float | None, limit: int = 3, window_days: int = 5):
        self.enforced = account_equity is not None and account_equity < 25_000
        self.limit, self.window = limit, window_days
        self._events: deque[pd.Timestamp] = deque()

    def _trim(self, today: pd.Timestamp):
        cutoff = pd.bdate_range(end=today, periods=self.window)[0]
        while self._events and self._events[0] < cutoff:
            self._events.popleft()

    def count(self, today: pd.Timestamp) -> int:
        self._trim(pd.Timestamp(today))
        return len(self._events)

    def can_day_trade(self, today: pd.Timestamp) -> bool:
        return (not self.enforced) or self.count(today) < self.limit

    def record(self, today: pd.Timestamp) -> None:
        self._events.append(pd.Timestamp(today))


class TaxLots:
    """FIFO lots; every round trip <= 1y is short-term at the ordinary rate (§H).
    after_tax_ret_t = ret_t - rate * realized_net_gain_t / NAV_t (losses offset)."""

    def __init__(self, rate: float | None):
        self.rate = rate or 0.0
        self._lots: dict[str, deque] = {}
        self.realized: dict[pd.Timestamp, float] = {}

    def buy(self, ticker: str, qty: float, price: float, date):
        self._lots.setdefault(ticker, deque()).append([qty, price])

    def sell(self, ticker: str, qty: float, price: float, date):
        date = pd.Timestamp(date)
        lots = self._lots.get(ticker)
        gain = 0.0
        remaining = qty
        while lots and remaining > 1e-12:
            lot = lots[0]
            take = min(lot[0], remaining)
            gain += take * (price - lot[1])
            lot[0] -= take
            remaining -= take
            if lot[0] <= 1e-12:
                lots.popleft()
        self.realized[date] = self.realized.get(date, 0.0) + gain
        return gain
