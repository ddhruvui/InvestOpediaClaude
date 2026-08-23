"""Synthetic M1 landing layer for end-to-end pipeline rehearsal (no vendor data).
Cross-sectional momentum signal planted so the pipeline has something to find."""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def build(out_dir: str, n_names: int = 40, years: float = 7.0, seed: int = 42,
          ar: float = 0.995, drift_scale: float = 0.00035):
    out = Path(out_dir)
    (out / "raw_prices_eod").mkdir(parents=True, exist_ok=True)
    (out / "adjustment_factors").mkdir(exist_ok=True)
    (out / "market").mkdir(exist_ok=True)
    rng = np.random.default_rng(seed)
    n_days = int(252 * years)
    idx = pd.bdate_range("2018-01-02", periods=n_days)
    tick = [f"S{i:03d}" for i in range(n_names)]

    # returns with a momentum tilt: names trend (AR in drift) -> 12-1 has signal
    drift = np.zeros((n_days, n_names))
    for j in range(n_names):
        mu = 0.0
        for i in range(n_days):
            mu = ar * mu + rng.normal(0, drift_scale)
            drift[i, j] = mu
    rets = drift + rng.normal(0.0002, 0.015, (n_days, n_names))
    close = 50 * np.exp(np.cumsum(rets, axis=0))
    op = close * (1 + rng.normal(0, 0.003, (n_days, n_names)))
    hi = np.maximum(close, op) * (1 + np.abs(rng.normal(0, 0.003, (n_days, n_names))))
    lo = np.minimum(close, op) * (1 - np.abs(rng.normal(0, 0.003, (n_days, n_names))))
    vol = rng.integers(int(5e5), int(5e7), (n_days, n_names)).astype(float)

    pd.DataFrame({"date": idx.strftime("%Y-%m-%d"), "is_early_close": False}) \
        .to_parquet(out / "sessions.parquet", index=False)
    rows = []
    for j, t in enumerate(tick):
        rows.append(pd.DataFrame({
            "date": idx, "ticker": t, "open": op[:, j], "high": hi[:, j],
            "low": lo[:, j], "close": close[:, j], "volume": vol[:, j],
            "close_fully_adjusted": close[:, j], "adjusted_close_vendor": close[:, j],
            "close_source": "synthetic", "quarantined": False,
            "pre_first_price_date": False}))
    px = pd.concat(rows, ignore_index=True)
    for y, g in px.groupby(px["date"].dt.year):
        g.to_parquet(out / "raw_prices_eod" / f"part-{y}.parquet", index=False)
    fac = px[["date", "ticker"]].copy()
    fac["factor"] = 1.0
    for y, g in fac.groupby(fac["date"].dt.year):
        g.to_parquet(out / "adjustment_factors" / f"part-{y}.parquet", index=False)
    pd.DataFrame(columns=["date", "ticker", "action_type", "value", "source"]) \
        .to_parquet(out / "corporate_actions.parquet", index=False)
    pd.DataFrame(columns=["ticker", "item", "fiscal_period", "filing_datetime",
                          "lastupdated", "dimension", "value"]) \
        .to_parquet(out / "fundamentals_pit.parquet", index=False)
    pd.DataFrame(columns=["ticker", "fiscal_period", "report_date", "before_after_market",
                          "eps_estimate", "eps_actual", "eps_difference",
                          "surprise_percent"]).to_parquet(out / "earnings_surprises.parquet",
                                                          index=False)
    pd.DataFrame(columns=["ticker", "period", "period_frequency", "as_of_date",
                          "eps_trend_current", "eps_trend_90d"]) \
        .to_parquet(out / "estimates_pit.parquet", index=False)
    pd.DataFrame({"ticker": tick, "name": tick, "category": "Domestic Common Stock",
                  "isdelisted": "N"}).to_parquet(out / "entities.parquet", index=False)
    # SPY = equal-weight market proxy
    spy_close = 100 * np.exp(np.cumsum(rets.mean(axis=1)))
    spy = [{"date": d.strftime("%Y-%m-%d"), "open": float(o), "close": float(c),
            "adjusted_close": float(c)}
           for d, o, c in zip(idx, spy_close * (1 + rng.normal(0, .002, n_days)), spy_close)]
    (out / "market" / "SPY.json").write_text(json.dumps(spy))
    (out / "_manifest.json").write_text(json.dumps({"data_snapshot_id": "synthetic"}))
    print(f"synth M1 at {out}: {n_days} sessions x {n_names} names")


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "scratch_m1")
