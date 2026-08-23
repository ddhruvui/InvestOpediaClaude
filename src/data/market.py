"""Read side of build_market.py output (m1x): survivorship-free workset prices +
dated universe membership. Sharadar-preferred raw closes / closeadj from the M1
per-ticker layer override the EODHD bulk columns where both exist (RULE 2)."""
from __future__ import annotations

import glob
from pathlib import Path

import numpy as np
import pandas as pd


def load_workset_prices(market_dir: str | Path,
                        m1_prices: pd.DataFrame | None = None,
                        since: pd.Timestamp | None = None) -> pd.DataFrame:
    """since: skip year-parts entirely before it (continual-learning tail window) —
    the parts are named part-YYYY so the filter avoids even reading old files."""
    parts = sorted(glob.glob(str(Path(market_dir) / "workset_prices" / "part-*.parquet")))
    if not parts:
        raise FileNotFoundError(f"no workset_prices under {market_dir}")
    if since is not None:
        y0 = pd.Timestamp(since).year
        parts = [p for p in parts if int(Path(p).stem.split("-")[1]) >= y0] or parts[-1:]
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["date"] = pd.to_datetime(df["date"])
    if since is not None:
        df = df[df["date"] >= pd.Timestamp(since)]
    out = df.rename(columns={"adjusted_close": "adjusted_close_vendor"})
    out["quarantined"] = False
    out["close_source"] = "eodhd_bulk"
    if m1_prices is not None and len(m1_prices):
        keep = ["date", "ticker", "close", "close_source", "close_fully_adjusted",
                "quarantined"]
        keep = [c for c in keep if c in m1_prices.columns]
        m1p = m1_prices[keep].rename(columns={
            "close": "m1_close", "close_source": "m1_close_source",
            "close_fully_adjusted": "m1_closeadj", "quarantined": "m1_quar"})
        out = out.merge(m1p, on=["date", "ticker"], how="left")
        sharadar = out.get("m1_close_source", pd.Series(index=out.index)) \
            .eq("sharadar_closeunadj")
        out.loc[sharadar, "close"] = out.loc[sharadar, "m1_close"]
        out.loc[sharadar, "close_source"] = "sharadar_closeunadj"
        if "m1_closeadj" in out:
            out["close_fully_adjusted"] = out["m1_closeadj"]
        if "m1_quar" in out:
            out["quarantined"] = out["m1_quar"].fillna(False).astype(bool)
            out.loc[out["quarantined"] & out["close_source"].eq("eodhd_bulk"),
                    "close"] = np.nan
        out = out.drop(columns=[c for c in ("m1_close", "m1_close_source", "m1_closeadj",
                                            "m1_quar") if c in out])
    # 16.8M rows: python-string columns cost GBs — categorize; floats to float32
    out["ticker"] = out["ticker"].astype("category")
    out["close_source"] = out["close_source"].astype("category")
    for c in ("open", "high", "low", "close", "adjusted_close_vendor", "volume",
              "close_fully_adjusted"):
        if c in out:
            out[c] = pd.to_numeric(out[c], errors="coerce").astype("float32")
    return out


FUND_CATEGORIES = ("ETF", "ETN", "ETD", "CEF", "IDX", "Fund")


def fund_tickers(entities: pd.DataFrame) -> set[str]:
    """Tickers whose Sharadar category marks them as funds/notes, not stocks (G-05)."""
    if entities is None or not len(entities) or "category" not in entities:
        return set()
    cat = entities["category"].fillna("")
    is_fund = cat.str.contains("|".join(FUND_CATEGORIES), case=False, regex=True)
    return set(entities.loc[is_fund, "ticker"].astype(str))


def membership_mask(market_dir: str | Path, dates: pd.DatetimeIndex,
                    tickers: pd.Index, exclude: set[str] | None = None) -> pd.DataFrame:
    """Monthly refresh rows -> daily bool mask; membership applies from the NEXT
    session after the refresh date (Q-003 no-look-ahead)."""
    mem = pd.read_parquet(Path(market_dir) / "universe_membership.parquet")
    mem["refresh_date"] = pd.to_datetime(mem["refresh_date"])
    mask = pd.DataFrame(False, index=dates, columns=tickers)
    refreshes = sorted(mem["refresh_date"].unique())
    for i, r in enumerate(refreshes):
        names = mem.loc[mem["refresh_date"] == r, "ticker"]
        start = dates.searchsorted(r) + 1
        end = dates.searchsorted(refreshes[i + 1]) + 1 if i + 1 < len(refreshes) else len(dates)
        if start >= len(dates):
            continue
        if exclude:
            names = [n for n in names if n not in exclude]
        cols = tickers.intersection(names)
        mask.iloc[start:end, [mask.columns.get_loc(c) for c in cols]] = True
    return mask
