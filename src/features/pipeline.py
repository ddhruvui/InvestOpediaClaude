"""M4 — feature assembly & post-processing (order matters, §F):

  1. winsorize each raw feature at 1st/99th pct WITHIN day [IMPL]
  2. cross-sectional normalize per Q-017 (`rank` default) over the universe mask
  3. NaN policy is the MODEL'S at load time (LGBM native NaN; GRU -> 0) — the
     matrix keeps NaN
  4. emit feature_manifest (name, block, formula hash) for drift detection
"""
from __future__ import annotations

import hashlib
import inspect

import numpy as np
import pandas as pd

from src.features import events as _ev
from src.features import fundamental as _fu
from src.features import technical as _te
from src.primitives.csnorm import cs_transform
from src.primitives.returns import daily_return, true_range
from src.primitives.rolling import RollingCache

FUND_KEYS = ("ep", "bm", "sp", "roe", "gross_prof", "asset_growth", "accruals", "size")


def _winsorize_day(x: pd.DataFrame, lo: float = 0.01, hi: float = 0.99) -> pd.DataFrame:
    ql = x.quantile(lo, axis=1)
    qh = x.quantile(hi, axis=1)
    return x.clip(lower=ql, upper=qh, axis=0)


def build_features(panel, mask: pd.DataFrame, fundamentals: pd.DataFrame | None = None,
                   surprises: pd.DataFrame | None = None,
                   estimates: pd.DataFrame | None = None,
                   sessions: pd.DatetimeIndex | None = None,
                   norm_mode: str = "rank") -> tuple[pd.DataFrame, dict]:
    """Returns (features_long, manifest). features_long: MultiIndex (date, ticker) rows,
    one column per normalized feature, restricted to the universe mask (M2-02)."""
    # float32 working set: with a ~4.6k-name workset the float64 wide frames peak
    # near 9 GB; float32 halves it with no effect on rank-normalized outputs [IMPL]
    from dataclasses import replace
    f32 = {f: getattr(panel, f).astype(np.float32) for f in
           ("adj_open", "adj_high", "adj_low", "adj_close", "adj_volume",
            "raw_volume", "raw_close", "raw_open")}
    panel = replace(panel, **f32)
    r = daily_return(panel.adj_close)
    tr = true_range(panel.adj_high, panel.adj_low, panel.adj_close)
    cache = RollingCache({"adj_close": panel.adj_close, "r": r,
                          "volume": panel.adj_volume, "TR": tr})

    # ---- streaming assembly: ONE wide frame alive at a time, written straight
    # into a preallocated float32 matrix over the masked (date, ticker) index ----
    in_univ = mask.stack(future_stack=True)
    long_index = in_univ.index[in_univ.to_numpy(dtype=bool)]

    def feature_stream():
        shares_pit = None
        if fundamentals is not None and len(fundamentals) and sessions is not None:
            fnd = _fu.fundamental_features(fundamentals, panel.raw_close, sessions)
            shares_pit = fnd.pop("_shares_pit")
            fnd.pop("_mcap")
            for name in list(fnd):
                yield "F7", name, fnd.pop(name)
        yield from (("F1-F6", n, w) for n, w in
                    _te.iter_technical_features(panel, r, tr, cache,
                                                shares_pit=shares_pit))
        if surprises is not None and sessions is not None:
            ev = _ev.event_features(surprises if len(surprises) else pd.DataFrame(),
                                    estimates, panel.raw_close, sessions)
            for name in list(ev):
                yield "F8", name, ev.pop(name)

    manifest, names, cols = {}, [], []
    for bname, name, wide in feature_stream():
        w = _winsorize_day(wide.astype(np.float32))
        del wide
        norm = cs_transform(w, mask, mode=norm_mode).astype(np.float32)
        del w
        cols.append(norm.stack(future_stack=True).reindex(long_index)
                    .to_numpy(dtype=np.float32))
        del norm
        names.append(name)
        manifest[name] = {"block": bname,
                          "formula_hash": hashlib.sha256(
                              f"{name}|{bname}|{norm_mode}".encode()).hexdigest()[:12]}

    long = pd.DataFrame(np.column_stack(cols), index=long_index, columns=names)
    del cols
    long.index.names = ["date", "ticker"]
    long = long.dropna(how="all")
    return long, manifest
