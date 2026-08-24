"""M18 — nightly order generation (§H, §I, BP16). RESEARCH/PAPER SIDE.

Turns M14 target weights into an order FILE (shares from RAW prices, G-03/M14-01)
with the M5.2 barrier orders attached. Actual submission to IBKR (ib_async) is a
separate, deliberately manual step: this module never places trades on its own.

Order sequence (§2.3 step 9): diff targets vs current book -> desired d-shares
using raw close [IMPL: floor(dw x NAV / raw_close)] -> PDT pre-check -> MOO
orders + GTC stop/profit-take for new entries -> vertical-barrier MOO schedule.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.backtest.compliance import PDTCounter


@dataclass
class Order:
    ticker: str
    action: str            # BUY / SELL
    quantity: int
    order_type: str        # MOO / STP / LMT
    tif: str               # DAY / GTC
    limit_price: float | None = None
    stop_price: float | None = None
    note: str = ""


def generate_orders(target_weights: pd.Series, current_shares: pd.Series,
                    raw_close: pd.Series, nav: float, sigma32: pd.Series,
                    cfg, pdt: PDTCounter | None = None,
                    as_of=None) -> list[Order]:
    m, h = float(cfg.barrier.m), int(cfg.barrier.h_days)
    cap = cfg.barrier.get("thr_cap_pct")
    orders: list[Order] = []
    for t, w in target_weights.items():
        px = raw_close.get(t, np.nan)
        if not np.isfinite(px) or px <= 0:
            continue
        tgt_sh = int(np.floor(w * nav / px))
        cur_sh = int(current_shares.get(t, 0))
        d = tgt_sh - cur_sh
        if d == 0:
            continue
        orders.append(Order(t, "BUY" if d > 0 else "SELL", abs(d), "MOO", "DAY",
                            note=f"target_w={w:.4f}"))
        if cur_sh == 0 and d > 0:            # new entry: attach barrier orders (§G)
            thr = m * float(sigma32.get(t, np.nan)) * np.sqrt(h)
            if cap is not None:
                thr = min(thr, float(cap))
            if np.isfinite(thr):
                orders.append(Order(t, "SELL", abs(d), "STP", "GTC",
                                    stop_price=round(px * (1 - thr), 2),
                                    note="M5.2 stop (re-peg to fill)"))
                orders.append(Order(t, "SELL", abs(d), "LMT", "GTC",
                                    limit_price=round(px * (1 + thr), 2),
                                    note=f"M5.2 profit-take; vertical MOO t+{h + 1}"))
    return orders


def write_order_file(orders: list[Order], out_path: str | Path, stamp: dict,
                     kill_switch_active: bool = False) -> None:
    payload = {"stamp": stamp, "kill_switch_active": kill_switch_active,
               "orders": [] if kill_switch_active else [asdict(o) for o in orders],
               "note": "Submission is manual. This file is research output, "
                       "not an instruction to trade."}
    Path(out_path).write_text(json.dumps(payload, indent=2))


def measure_slippage(fills: pd.DataFrame, official_open: pd.Series) -> pd.DataFrame:
    """Per fill: slip_bps = side x (fill - official_open)/official_open x 1e4 (§H).
    Rolling median feeds M15-03 once >= 60 fills exist."""
    df = fills.copy()
    op = official_open.reindex(df["ticker"]).to_numpy()
    df["slip_bps"] = np.sign(df["qty"]) * (df["price"] - op) / op * 1e4
    return df


def kill_switch(daily_pnl_frac: float, cfg) -> bool:
    """BP16: realized day loss <= -ops.max_daily_loss_pct x NAV -> halt new orders."""
    return daily_pnl_frac <= -float(cfg.ops.max_daily_loss_pct)


def decay_monitor(live_daily: pd.Series, backtest_sharpe: float,
                  window: int = 63) -> dict:
    """G-11/G-16: rolling 63d live Sharpe < 1/2 backtest for 2+ quarters ->
    retrain-or-retire."""
    from src.validation.metrics import sharpe
    if len(live_daily) < window:
        return {"status": "insufficient_history", "n": len(live_daily)}
    roll = live_daily.rolling(window).apply(
        lambda x: np.sqrt(252) * x.mean() / x.std() if x.std() > 0 else np.nan)
    breach = roll < 0.5 * backtest_sharpe
    consec = breach[::-1].cumprod().sum() if len(breach) else 0
    return {"status": "retire_or_retrain" if consec >= 126 else "healthy",
            "rolling_sharpe_last": float(roll.iloc[-1]) if np.isfinite(roll.iloc[-1]) else None,
            "sessions_in_breach": int(consec)}
