"""M15 — event-driven confirmation engine with LIVE barrier exits (Stage 3, §J).

Positions have individual lifecycles: a new 1/`tranches` NAV tranche enters each
day (decile-10 + meta gate, inverse-vol weights), fills at open t+1, and exits
via the ONE M5.2 barrier engine (stop / profit-take intraday, vertical MOO at
t+h+1) — never a re-implementation (M15-02/G-15). Same-session exits increment
the PDT counter; under $25k the exhausted 4th converts to next-open deferral
[IMPL pdt.mode_under_25k]. Gap-throughs fill at the session open (M15-04).

This engine cross-checks the fast path within |dSharpe| <= 0.1 tolerance [IMPL].
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.backtest.compliance import PDTCounter
from src.backtest.costs import CostModel
from src.labels.barriers import barrier_exits


def run_event_backtest(selection: pd.DataFrame, panel, sigma32: pd.DataFrame,
                       cost_model: CostModel, cfg,
                       meta_mult: pd.DataFrame | None = None,
                       account_equity: float | None = None,
                       day_budget_mult: pd.Series | None = None,
                       nav0: float = 1.0, m: float | None = None,
                       h: int | None = None,
                       thr_cap: float | None = None,
                       m_up: float | None = None,
                       m_dn: float | None = None,
                       net_moo_costs: bool = False,
                       gross_cap: float | None = None) -> dict:
    """selection: wide bool frame (decision date x ticker) of names entering that
    day's tranche. Returns {'daily_net', 'equity', 'trades', 'pdt_log', ...}."""
    dates = panel.adj_open.index
    O = panel.adj_open
    pos = {d: i for i, d in enumerate(dates)}
    tranches = int(cfg.port.tranches)
    cap = float(cfg.port.single_name_cap)
    m_b = float(cfg.barrier.m) if m is None else float(m)
    h_b = int(cfg.barrier.h_days) if h is None else int(h)
    if thr_cap is None:
        thr_cap = cfg.barrier.get("thr_cap_pct")

    # 1) entries per decision day with inverse-vol weights inside the tranche
    #
    # gross_cap: hard ceiling on PROJECTED live gross (sum of open tranche
    # weights, assuming each runs to its vertical exit — early barrier exits
    # only free capital sooner, so the projection is conservative). The
    # entering tranche is scaled down to fit; a Reg-T margin account cannot
    # follow the uncapped vol-target through low-vol regimes.
    entries = []
    live_q: list[tuple[int, float]] = []   # (expiry index, entering gross)
    live_gross = 0.0
    for d0, row in selection.iterrows():
        names = list(row.index[row.astype(bool)])
        if not names:
            continue
        iv = 1.0 / sigma32.loc[d0, names]
        if meta_mult is not None:
            mm = meta_mult.reindex(index=[d0], columns=names).iloc[0].fillna(0.0)
            iv = iv * mm
        iv = iv.replace([np.inf, -np.inf], np.nan).dropna()
        iv = iv[iv > 0]
        if not len(iv):
            continue
        w = (iv / iv.sum()).clip(upper=cap * tranches)   # cap at book level
        # M13 regime overlay + M14 vol targeting scale the ENTERING tranche
        bud = float(day_budget_mult.get(d0, 1.0)) if day_budget_mult is not None else 1.0
        if gross_cap is not None:
            i_d = pos.get(pd.Timestamp(d0))
            if i_d is not None:
                while live_q and live_q[0][0] <= i_d:
                    live_gross -= live_q.pop(0)[1]
                intended = float(w.sum()) / tranches * bud
                allowed = max(0.0, float(gross_cap) - live_gross)
                if intended > allowed:
                    bud *= allowed / intended if intended > 0 else 0.0
                    intended = allowed
                if intended > 0:
                    live_q.append((i_d + h_b + 1, intended))
                    live_gross += intended
                if bud <= 0:
                    continue
        for t, wt in w.items():
            entries.append({"date": d0, "ticker": t, "side": 1,
                            "tranche_w": wt / tranches * bud})
    edf = pd.DataFrame(entries)
    if edf.empty:
        z = pd.Series(0.0, index=dates)
        return {"daily_net": z, "equity": (1 + z).cumprod(), "trades": edf,
                "pdt_log": [], "n_trades": 0, "avg_hold": float("nan"),
                "hit_counts": {}}

    # 2) exits via the ONE barrier engine (C-06)
    ex = barrier_exits(edf[["date", "ticker", "side"]], panel.adj_open, panel.adj_high,
                       panel.adj_low, panel.adj_close, sigma32, cost_model,
                       m=m_b, h=h_b, tie_break=str(cfg.barrier.tie_break),
                       thr_cap=thr_cap, m_up=m_up, m_dn=m_dn)
    ex["tranche_w"] = edf["tranche_w"].to_numpy()
    ex = ex[ex["barrier_hit"].isin(["upper", "lower", "vertical", "censored"])]

    # 3) PDT: same-session exits; under $25k the exhausted 4th defers to next open
    pdt = PDTCounter(account_equity)
    pdt_log = []
    Ov = O.to_numpy()
    cols = {t: j for j, t in enumerate(O.columns)}
    day_rows = ex.index[ex["day_trade"]]
    for i in day_rows:
        d_fill = ex.at[i, "fill_date"]
        if pdt.can_day_trade(d_fill):
            pdt.record(d_fill)
            pdt_log.append({"date": str(d_fill), "action": "day_trade_allowed"})
        else:
            j, ifill = cols[ex.at[i, "ticker"]], pos[d_fill]
            if ifill + 1 < len(dates) and np.isfinite(Ov[ifill + 1, j]):
                ex.at[i, "exit_date"] = dates[ifill + 1]
                ex.at[i, "exit_price"] = Ov[ifill + 1, j]
                g = ex.at[i, "side"] * (ex.at[i, "exit_price"] / ex.at[i, "entry_price"] - 1)
                ex.at[i, "exit_ret_gross"] = g
                ex.at[i, "exit_ret_net"] = g - cost_model.round_trip_frac(
                    side=int(ex.at[i, "side"]), holding_days=1)
                ex.at[i, "day_trade"] = False
                pdt_log.append({"date": str(d_fill), "action": "deferred_to_next_open"})

    # 4) mark-to-market daily P&L per position -> book daily returns (compounded
    #    on the tranche budget; costs at entry+exit legs are inside exit_ret_net)
    #
    # net_moo_costs: entries fill MOO, and vertical exits leave MOO at the SAME
    # open print — a live order file nets the two per ticker, so only the NET
    # traded notional pays the leg cost. Per-tranche accounting otherwise charges
    # a persistent name a full round trip every h sessions for re-entering
    # itself, which at short h is the dominant (and fictional) cost.
    # Intraday barrier exits (upper/lower/censored) cannot net against a MOO
    # entry and always pay their leg. Default False = original accounting.
    pnl = np.zeros(len(dates))
    leg = cost_model.leg_frac()
    buy_open: dict[tuple[int, int], float] = {}    # (day, ticker) MOO buys
    sell_open: dict[tuple[int, int], float] = {}   # (day, ticker) MOO sells
    cost = np.zeros(len(dates))
    for r in ex.itertuples(index=False):
        i0, i1 = pos[r.fill_date], pos[r.exit_date]
        j = cols[r.ticker]
        path = Ov[i0:i1 + 1, j].copy()
        path[-1] = r.exit_price
        rets = np.diff(path) / path[:-1]
        if i1 > i0:
            pnl[i0 + 1:i1 + 1] += r.tranche_w * rets
        else:   # same-session round trip: open->barrier within the fill session
            pnl[i0] += r.tranche_w * (r.exit_price / r.entry_price - 1)
        exit_at_open = i1 > i0 and r.exit_price == Ov[i1, j]
        if net_moo_costs:
            buy_open[(i0, j)] = buy_open.get((i0, j), 0.0) + r.tranche_w
            if exit_at_open:
                sell_open[(i1, j)] = sell_open.get((i1, j), 0.0) + r.tranche_w
            else:
                cost[i1] += r.tranche_w * leg
        else:
            # inline application preserves the original float summation order —
            # the default path stays bit-identical to the pre-netting engine
            pnl[i0] -= r.tranche_w * leg
            pnl[i1] -= r.tranche_w * leg
            cost[i0] += r.tranche_w * leg
            cost[i1] += r.tranche_w * leg
    if net_moo_costs:
        for (i, j), w in buy_open.items():
            net_w = w - sell_open.pop((i, j), 0.0)
            cost[i] += abs(net_w) * leg
        for (i, j), w in sell_open.items():   # MOO sells with no same-day buy
            cost[i] += w * leg
        pnl -= cost
    daily = pd.Series(pnl, index=dates)
    equity = (1 + daily).cumprod() * nav0
    return {"daily_net": daily, "equity": equity, "trades": ex, "pdt_log": pdt_log,
            "n_trades": int(len(ex)),
            "avg_hold": float(ex["holding_days"].mean()),
            "cost_daily": pd.Series(cost, index=dates),
            "hit_counts": ex["barrier_hit"].value_counts().to_dict()}
