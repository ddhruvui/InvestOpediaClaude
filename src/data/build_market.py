#!/usr/bin/env python3
"""Whole-market panel + survivorship-free universe (G-05/BP1) from eod_bulk.

Runs ON the pod (the day-files only exist on the volume). Steps:

 1. data/eod_bulk/US/<DATE>.json  ->  market_prices/part-YYYY.parquet
    (date, ticker, open, high, low, close, adjusted_close, volume) — every
    listed AND delisted US name, the survivorship-free price record.
 2. 63d rolling median dollar volume per name -> monthly top-N membership with
    10% hysteresis + min_price filter (Q-003 mechanics, next-session inclusion
    applied downstream) -> universe_membership.parquet (date, ticker  rows at
    monthly refresh granularity).
 3. Workset = every name ever selected  (+ Sharadar SP500 ever-members)  ->
    workset_prices/part-YYYY.parquet — the compact panel Stage 1+ actually loads.

Outputs land in MARKET_DIR (default /workspace/m1x). EODHD `adjusted_close` is
the fallback total-return factor source for names Sharadar doesn't cover; the
M1 adjustment_factors override where present (handled at panel-build time).

Env: EOD_BULK_DIR, MARKET_DIR, M1_DIR, UNIVERSE_SIZE, MIN_PRICE.
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

BULK_DIR = Path(os.environ.get("EOD_BULK_DIR", "/workspace/data/eod_bulk/US"))
MARKET_DIR = Path(os.environ.get("MARKET_DIR", "/workspace/m1x"))
M1_DIR = Path(os.environ.get("M1_DIR", "/workspace/m1"))
NASDAQ_DIR = Path(os.environ.get("NASDAQ_DIR", "/workspace/data_nasdaq"))
UNIVERSE_SIZE = int(os.environ.get("UNIVERSE_SIZE", "1000"))
MIN_PRICE = float(os.environ.get("MIN_PRICE", "5.0"))
HYSTERESIS = float(os.environ.get("HYSTERESIS", "0.10"))
DV_WINDOW = int(os.environ.get("DV_WINDOW", "63"))
PRUNE_K = int(os.environ.get("PRUNE_K", "3000"))

KEEP = ["date", "ticker", "open", "high", "low", "close", "adjusted_close", "volume"]


def _load_json(p: str):
    try:
        if p.endswith(".gz"):
            with gzip.open(p, "rt", encoding="utf-8") as f:
                return json.load(f)
        with open(p) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        print(f"WARN unreadable {p}: {e}", flush=True)
        return None


def step1_market_prices() -> list[int]:
    """Day-files -> per-year parquet. Skips years already built and complete."""
    (MARKET_DIR / "market_prices").mkdir(parents=True, exist_ok=True)
    files = sorted(glob.glob(str(BULK_DIR / "*.json"))
                   + glob.glob(str(BULK_DIR / "*.json.gz")))
    by_year: dict[int, list[str]] = defaultdict(list)
    for p in files:
        d = os.path.basename(p).split(".")[0]
        if len(d) == 10:
            by_year[int(d[:4])].append(p)
    years = sorted(by_year)
    marker = MARKET_DIR / "market_prices" / "_built.json"
    built = json.loads(marker.read_text()) if marker.exists() else {}
    for y in years:
        n_files = len(by_year[y])
        if str(y) in built and built[str(y)] == n_files:
            continue
        frames = []
        for p in by_year[y]:
            rows = _load_json(p)
            if not rows:
                continue
            df = pd.DataFrame(rows)
            code = df.get("code", df.get("ticker"))
            date = df.get("date")
            if date is None:      # some vintages carry only the filename date
                date = os.path.basename(p).split(".")[0]
            out = pd.DataFrame({
                "date": date, "ticker": code,
                "open": pd.to_numeric(df.get("open"), errors="coerce"),
                "high": pd.to_numeric(df.get("high"), errors="coerce"),
                "low": pd.to_numeric(df.get("low"), errors="coerce"),
                "close": pd.to_numeric(df.get("close"), errors="coerce"),
                "adjusted_close": pd.to_numeric(df.get("adjusted_close"), errors="coerce"),
                "volume": pd.to_numeric(df.get("volume"), errors="coerce"),
            })
            frames.append(out.dropna(subset=["close"]))
        if frames:
            yr = pd.concat(frames, ignore_index=True)
            yr["date"] = yr["date"].astype(str).str[:10]
            yr = yr.drop_duplicates(["date", "ticker"], keep="last")
            yr[KEEP].to_parquet(MARKET_DIR / "market_prices" / f"part-{y}.parquet",
                                index=False)
            built[str(y)] = n_files
            marker.write_text(json.dumps(built))
            print(f"OK market_prices {y}: {len(yr):,} rows from {n_files} day-files",
                  flush=True)
    return years


def step2_universe(years: list[int]) -> pd.DataFrame:
    """Monthly top-N by 63d median dollar volume, hysteresis, min_price."""
    cols = ["date", "ticker", "close", "volume"]
    dv_parts, px_me_parts = [], []
    for y in years:
        p = MARKET_DIR / "market_prices" / f"part-{y}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p, columns=cols)
        df["date"] = pd.to_datetime(df["date"])
        df["dv"] = df["close"] * df["volume"]
        # PRUNE: a name outside this year's top PRUNE_K by median dv cannot reach a
        # 63d-median top-UNIVERSE_SIZE rank; 3x buffer, logged [IMPL memory guard]
        keep = (df.groupby("ticker")["dv"].median()
                  .nlargest(max(PRUNE_K, UNIVERSE_SIZE * 3)).index)
        df = df[df["ticker"].isin(keep)]
        # per-year pivots, float32 — full-market wide in float64 would be ~10 GB
        dv_parts.append(df.pivot_table(index="date", columns="ticker", values="dv",
                                       aggfunc="last").astype(np.float32))
        pxy = df.pivot_table(index="date", columns="ticker", values="close",
                             aggfunc="last").astype(np.float32)
        px_me_parts.append(pxy.groupby([pxy.index.year, pxy.index.month]).tail(1))
        del df, pxy
    wide = pd.concat(dv_parts).sort_index()
    del dv_parts
    px_me = pd.concat(px_me_parts).sort_index()
    del px_me_parts
    print(f"dv panel: {wide.shape[0]:,} sessions x {wide.shape[1]:,} names", flush=True)
    # rolling median in COLUMN CHUNKS, keeping only month-end rows (4 GB pod budget)
    me_mask = wide.index.isin(
        wide.index.to_series().groupby([wide.index.year, wide.index.month]).max())
    me_rows = wide.index[me_mask]
    med_cols = []
    CHUNK = 4000
    for c0 in range(0, wide.shape[1], CHUNK):
        block = wide.iloc[:, c0:c0 + CHUNK]
        med_cols.append(block.rolling(DV_WINDOW, min_periods=DV_WINDOW // 2)
                        .median().loc[me_rows].astype(np.float32))
    me = pd.concat(med_cols, axis=1)
    del med_cols, wide
    members = []
    current: set[str] = set()
    for d, row in me.iterrows():
        m = row.dropna()
        m = m[px_me.loc[d].reindex(m.index) >= MIN_PRICE]
        if m.empty:
            continue
        rank = m.rank(ascending=False, method="first")
        entrants = set(rank[rank <= UNIVERSE_SIZE].index)
        stay = {t for t in current if rank.get(t, np.inf) <= UNIVERSE_SIZE * (1 + HYSTERESIS)}
        current = entrants | stay
        members.append(pd.DataFrame({"refresh_date": d, "ticker": sorted(current)}))
    mem = pd.concat(members, ignore_index=True)
    mem.to_parquet(MARKET_DIR / "universe_membership.parquet", index=False)
    print(f"universe_membership: {len(mem):,} rows over "
          f"{mem['refresh_date'].nunique()} refreshes, "
          f"{mem['ticker'].nunique():,} distinct names ever", flush=True)
    return mem


def step3_workset(years: list[int], mem: pd.DataFrame) -> None:
    workset = set(mem["ticker"].unique())
    sp = _load_json(str(NASDAQ_DIR / "SP500" / "SHARADAR.json"))
    if sp:
        workset |= {r.get("ticker") for r in sp if r.get("ticker")}
    print(f"workset: {len(workset):,} names", flush=True)
    (MARKET_DIR / "workset_prices").mkdir(exist_ok=True)
    for y in years:
        p = MARKET_DIR / "market_prices" / f"part-{y}.parquet"
        if not p.exists():
            continue
        df = pd.read_parquet(p)
        df = df[df["ticker"].isin(workset)]
        df.to_parquet(MARKET_DIR / "workset_prices" / f"part-{y}.parquet", index=False)
    (MARKET_DIR / "_manifest.json").write_text(json.dumps({
        "universe_size": UNIVERSE_SIZE, "min_price": MIN_PRICE,
        "hysteresis": HYSTERESIS, "dv_window": DV_WINDOW,
        "workset_names": len(workset)}))
    print("workset_prices written", flush=True)


def main() -> int:
    if not BULK_DIR.exists():
        print(f"FATAL: no eod_bulk at {BULK_DIR}", file=sys.stderr)
        return 1
    years = step1_market_prices()
    if not years:
        print("FATAL: no day-files parsed", file=sys.stderr)
        return 1
    mem = step2_universe(years)
    step3_workset(years, mem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
