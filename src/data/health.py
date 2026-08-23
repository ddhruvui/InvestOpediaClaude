"""M2 — Q-004 pre-run health report (BP16). Any MUST-level failure blocks the run."""
from __future__ import annotations

import numpy as np
import pandas as pd


def health_report(panel, universe_mask: pd.DataFrame | None = None,
                  expected_universe_band: tuple[int, int] = (300, 1100),
                  stale_run: int = 5) -> dict:
    """Panel-level checks. Returns {'checks': {...}, 'blocking': bool}."""
    checks: dict[str, dict] = {}
    ac = panel.adj_close.astype(np.float32)
    rc = panel.raw_close.astype(np.float32)

    # 1. Missing bars vs calendar. The MUST-level check counts only IN-UNIVERSE
    # gaps (universe names are liquid by construction; a hole there is a data
    # fault). Whole-lifespan gaps (OTC tails, halts of ex-universe names) are
    # reported as INFO.
    miss = {}
    for t in ac.columns:
        s = ac[t]
        alive = s.loc[s.first_valid_index():s.last_valid_index()] if s.notna().any() else s
        n = int(alive.isna().sum())
        if n:
            miss[t] = n
    checks["missing_bars_lifespan"] = {"tickers": len(miss),
                                       "total": sum(miss.values()),
                                       "ok": True, "level": "INFO"}
    if universe_mask is not None:
        in_univ_missing = int((universe_mask & ac.isna()).sum().sum())
        in_univ_total = max(1, int(universe_mask.sum().sum()))
        frac = in_univ_missing / in_univ_total
        checks["missing_bars_in_universe"] = {
            "total": in_univ_missing, "frac": round(frac, 5),
            "ok": frac < 0.02, "level": "MUST"}

    # 2. Stale prices: >= `stale_run` identical consecutive closes.
    eq = (ac.diff() == 0)
    run = eq.rolling(stale_run - 1).sum() == (stale_run - 1)
    n_stale = int(run.sum().sum())
    checks["stale_prices"] = {"bars": n_stale, "ok": True, "level": "SHOULD"}  # flag, not block

    # 3. adj/raw ratio continuity except at action dates (big jumps = suspect factor).
    with np.errstate(all="ignore"):
        rho = (ac / rc)
        jump = (rho / rho.shift() - 1).abs()
    n_jumps = int((jump > 0.005).sum().sum())     # non-action-day factor moves >0.5%
    checks["factor_continuity"] = {"jump_bars": n_jumps,
                                   "ok": n_jumps < 0.01 * ac.shape[0] * ac.shape[1],
                                   "level": "MUST"}

    # 4. Duplicate (date,ticker) impossible in wide form — checked at M1 read.
    # 5. Universe count in band.
    if universe_mask is not None:
        cnt = universe_mask.sum(axis=1)
        active = cnt[cnt > 0]
        ok = bool(len(active)) and active.iloc[-1] >= expected_universe_band[0] \
            and active.max() <= expected_universe_band[1]
        checks["universe_count"] = {"min": int(active.min()) if len(active) else 0,
                                    "max": int(active.max()) if len(active) else 0,
                                    "last": int(active.iloc[-1]) if len(active) else 0,
                                    "ok": ok, "level": "MUST"}

    # 6. Quarantine visibility (informational — quarantined closes already nulled in M1).
    checks["quarantined_bars"] = {"bars": int(panel.quarantined.sum().sum()),
                                  "ok": True, "level": "INFO"}

    blocking = any(not c["ok"] for c in checks.values() if c.get("level") == "MUST")
    return {"checks": checks, "blocking": blocking}
