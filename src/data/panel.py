"""M2 — adjusted price panel (Q-002 application) and the two price worlds (G-03).

Builds wide (date x ticker) frames from the M1 long tables:

  adj_open/high/low/close : split- AND dividend-adjusted (total-return) — the ONLY
                            price series features/labels/vol/analytics may read.
  adj_volume              : split-only adjusted volume (Q-002: inverse split factor;
                            dividends and spinoff price adjustments do NOT touch volume).
  raw_close/raw_open      : unadjusted — share counts, order generation, mcap only.

adj_close preference order per (date,ticker) row:
  1. Sharadar closeadj                      (vendor-consistent total-return, RULE Q-002)
  2. raw_close x Sharadar closeadj/closeunadj factor (same thing via the factor table)
  3. EODHD adjusted_close_vendor            (fallback only; mutable-close caveat)
Intraday fields are scaled by the row's own adj_close/raw_close ratio.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Panel:
    """The M2 output contract consumed by M3+ (wide frames share one index/columns)."""
    sessions: pd.DatetimeIndex          # full Q-001 grid (may extend past data)
    adj_open: pd.DataFrame
    adj_high: pd.DataFrame
    adj_low: pd.DataFrame
    adj_close: pd.DataFrame
    adj_volume: pd.DataFrame
    raw_volume: pd.DataFrame
    raw_close: pd.DataFrame
    raw_open: pd.DataFrame
    quarantined: pd.DataFrame           # bool mask of vendor-disagreement bars
    meta: dict = field(default_factory=dict)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.adj_close.index

    @property
    def tickers(self) -> pd.Index:
        return self.adj_close.columns


def _wide(df: pd.DataFrame, col: str, dates: pd.DatetimeIndex) -> pd.DataFrame:
    w = df.pivot_table(index="date", columns="ticker", values=col, aggfunc="last")
    return w.reindex(dates)


def split_volume_factor(actions: pd.DataFrame, dates: pd.DatetimeIndex,
                        tickers: pd.Index) -> pd.DataFrame:
    """Cumulative split-only volume multiplier per (date,ticker).

    After a k:1 split at ex-date s, volume before s is multiplied by k so share
    counts are comparable across the boundary (inverse of the price factor).
    Spinoff rows are price adjustments, not share-count changes — excluded (RULE 6).
    Dedupe (ticker,date): Sharadar preferred over EODHD when both report the split.
    """
    out = pd.DataFrame(1.0, index=dates, columns=tickers)
    if actions.empty:
        return out
    sp = actions[actions["action_type"] == "split"].copy()
    if sp.empty:
        return out
    pref = {"sharadar_actions": 0, "eodhd_splits": 1}
    sp["_pref"] = sp["source"].map(pref).fillna(2)
    sp = (sp.sort_values(["ticker", "date", "_pref"])
            .drop_duplicates(subset=["ticker", "date"], keep="first"))
    for t, g in sp.groupby("ticker"):
        if t not in out.columns:
            continue
        ratios = pd.to_numeric(g["value"], errors="coerce").dropna()
        ex = g.loc[ratios.index, "date"]
        col = np.ones(len(dates))
        for d, r in zip(ex, ratios):
            if r and r > 0:
                col[dates < d] *= r      # pre-split volume scaled up by the ratio
        out[t] = col
    return out


def build_panel(raw_prices: pd.DataFrame, factors: pd.DataFrame, actions: pd.DataFrame,
                sessions: pd.DatetimeIndex, tickers: list[str] | None = None) -> Panel:
    """Assemble the Panel from M1 tables (see module docstring for preference order)."""
    need = [c for c in ("date", "ticker", "open", "high", "low", "close", "volume",
                        "close_fully_adjusted", "adjusted_close_vendor", "quarantined")
            if c in raw_prices.columns]
    px = raw_prices[need]
    if tickers is not None:
        px = px[px["ticker"].isin(tickers)]
    px = px[px["date"].isin(sessions)]
    dates = pd.DatetimeIndex(sorted(px["date"].unique()))

    raw_close = _wide(px, "close", dates)
    raw_open = _wide(px, "open", dates)
    raw_high = _wide(px, "high", dates)
    raw_low = _wide(px, "low", dates)
    volume = _wide(px, "volume", dates)
    quar = _wide(px.assign(q=px["quarantined"].astype(float)), "q", dates).fillna(0.0) > 0

    # ---- adj_close preference chain ----
    adj_close = _wide(px, "close_fully_adjusted", dates) if "close_fully_adjusted" in px else \
        pd.DataFrame(np.nan, index=dates, columns=raw_close.columns)
    adj_close = adj_close.reindex(columns=raw_close.columns)
    n1 = int(adj_close.notna().sum().sum())
    if not factors.empty:
        f = factors[factors["ticker"].isin(raw_close.columns)]
        fw = _wide(f, "factor", dates).reindex(columns=raw_close.columns)
        adj_close = adj_close.where(adj_close.notna(), raw_close * fw)
    n2 = int(adj_close.notna().sum().sum())
    if "adjusted_close_vendor" in px:
        av = _wide(px, "adjusted_close_vendor", dates).reindex(columns=raw_close.columns)
        adj_close = adj_close.where(adj_close.notna(), av)
    n3 = int(adj_close.notna().sum().sum())

    # Per-row ratio scales intraday fields; volume gets the split-only inverse factor.
    with np.errstate(divide="ignore", invalid="ignore"):
        rho = adj_close / raw_close
    adj_open, adj_high, adj_low = raw_open * rho, raw_high * rho, raw_low * rho
    vf = split_volume_factor(actions, dates, raw_close.columns)
    adj_volume = volume * vf

    # ---- bar sanitation (Q-004 spirit, whole-market reality) ----
    # validate.py quarantines vendor disagreements only for the per-ticker names;
    # bulk-only names can carry corrupt prints (open 0.01 vs close 50) that fake
    # 1000x open-to-open round trips. A same-session open/close divergence beyond
    # 3x, or high/low inverted by >25%, is a data error, not a market move —
    # null the whole bar (engine treats it as a halt). Real crashes (open and
    # close collapsing TOGETHER) are untouched. [IMPL]
    with np.errstate(divide="ignore", invalid="ignore"):
        oc = adj_open / adj_close
        hl = adj_high / adj_low
    bad = (oc > 3.0) | (oc < 1.0 / 3.0) | (hl < 0.75)
    # VINTAGE SEAMS: eod_bulk day-files freeze a MIX of adjustment vintages, so a
    # split between two files' pull dates fakes a k-times jump in the ADJUSTED
    # series while the RAW print is smooth. Real actions have the OPPOSITE
    # fingerprint (raw jumps, adjusted smooth) and real crashes move both.
    # Null adj bars where adj moves >40% day-over-day but raw moves <15%. [IMPL]
    for adj_f, raw_f in ((adj_close, raw_close), (adj_open, raw_open)):
        with np.errstate(divide="ignore", invalid="ignore"):
            a_ret = adj_f / adj_f.shift(1) - 1
            r_ret = raw_f / raw_f.shift(1) - 1
        bad |= (a_ret.abs() > 0.40) & (r_ret.abs() < 0.15)
    # V-SPIKES: a bar that jumps >2x (either direction) and fully REVERTS on the
    # next bar is a bad print (mis-scaled feed row), not a market move — real
    # crashes do not round-trip to within 25% the next session. This looks ONE
    # bar ahead, which is TAPE CLEANING, not labeling: live operation applies it
    # with a one-session delay, and nulling makes the bar untradeable in BOTH
    # directions (removes fake P&L, never creates it). Numpy slicing on purpose —
    # the T-01 negative-shift containment guard stays reserved for label code. [IMPL]
    rc = raw_close.to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        jump = np.abs(rc[1:-1] / rc[:-2])          # bar t vs t-1
        rev = np.abs(rc[2:] / rc[:-2])             # bar t+1 vs t-1
    vs = np.zeros_like(rc, dtype=bool)
    vs[1:-1] = ((jump > 2.0) | (jump < 0.5)) & (rev < 1.25) & (rev > 0.8)
    bad |= pd.DataFrame(vs, index=bad.index, columns=bad.columns)
    # SYMBOL COLLISIONS / LEVEL FLIPS: two instruments interleaving under one
    # code print at wildly different levels (e.g. 8,200 vs 0.16). Null bars
    # whose log-price deviates >4x from the CENTERED 21-bar rolling median —
    # a real crash relocates the median with it; a feed mix-up cannot. Centered
    # window = tape cleaning (one-sided live equivalent lags 10 sessions). [IMPL]
    with np.errstate(divide="ignore", invalid="ignore"):
        lp = pd.DataFrame(np.log(rc), index=bad.index, columns=bad.columns)
    med = lp.rolling(21, center=True, min_periods=7).median()
    bad |= (lp - med).abs() > np.log(4.0)
    # TAPE BREAKS (symbol recycling): a >2.5x PERMANENT level shift with a >8x
    # volume-regime shift and no corporate action is a different instrument
    # under the same code. All bars FROM the break are nulled — the position
    # liquidates at the last good print (M1-02 delisting-style handling). Real
    # crashes keep their volume regime; real splits carry an action row. [IMPL]
    split_dates: dict[str, set] = {}
    if not actions.empty:
        sp_act = actions[actions["action_type"].isin(["split", "spinoff"])]
        for t_, g_ in sp_act.groupby("ticker"):
            split_dates[t_] = set(pd.to_datetime(g_["date"]).values)
    V = volume.to_numpy()
    date_vals = bad.index.values
    n_breaks = 0
    for j, t_ in enumerate(bad.columns):
        col = rc[:, j]
        ok = np.isfinite(col) & (col > 0)
        if ok.sum() < 60:
            continue
        lr_j = np.abs(np.diff(np.log(col)))
        cands = np.where(ok[1:] & ok[:-1] & (lr_j > np.log(2.5)))[0] + 1
        for i in cands:
            if any(abs((pd.Timestamp(date_vals[i]) - pd.Timestamp(dd)).days) <= 3
                   for dd in split_dates.get(t_, ())):
                continue
            after = col[i:i + 5]
            if np.isfinite(after).sum() < 3 or                not np.nanmedian(np.abs(after / col[i] - 1)) < 0.30:
                continue                                  # not persistent -> V-screen turf
            vb = np.nanmedian(V[max(0, i - 21):i, j])
            va = np.nanmedian(V[i:i + 21, j])
            if vb > 0 and va > 0 and (va / vb > 8 or va / vb < 1 / 8):
                bad.iloc[i:, j] = True
                n_breaks += 1
                break
    if n_breaks:
        print(f"panel: {n_breaks} tape breaks nulled-forward (symbol recycling)",
              flush=True)
    n_bad = int(bad.sum().sum())
    if n_bad:
        for f in (adj_open, adj_high, adj_low, adj_close):
            f[bad] = np.nan
        quar = quar | bad

    return Panel(
        sessions=sessions, adj_open=adj_open, adj_high=adj_high, adj_low=adj_low,
        adj_close=adj_close, adj_volume=adj_volume, raw_volume=volume,
        raw_close=raw_close, raw_open=raw_open,
        quarantined=quar,
        meta={"n_adj_from_sharadar_closeadj": n1, "n_adj_from_factor": n2 - n1,
              "n_adj_from_eodhd_vendor": n3 - n2,
              "n_bad_bars_nulled": n_bad,
              "n_price_rows": int(raw_close.notna().sum().sum())},
    )
