"""M16.3 — G-11 go/no-go gates, evaluated verbatim; sanity ceiling routes to the
leakage checklist (§9) instead of the results deck."""
from __future__ import annotations

import numpy as np


def evaluate_gates(rank_ic: float, sharpe_net: float, mdd: float,
                   beats_spy: bool, beats_mom: bool,
                   cpcv_path_sharpes: list[float] | None = None,
                   cpcv_path_mdds: list[float] | None = None) -> dict:
    kill = (not np.isfinite(rank_ic)) or (not np.isfinite(sharpe_net)) \
        or (rank_ic < 0.02) or (sharpe_net < 0.5)
    advance = (sharpe_net >= 0.8) and (mdd > -0.15) and beats_spy and beats_mom
    if cpcv_path_sharpes:
        advance &= bool(np.median(cpcv_path_sharpes) >= 0.8)
    if cpcv_path_mdds:
        advance &= bool(min(cpcv_path_mdds) > -0.15)
    sanity_ceiling = (sharpe_net > 2.0) or (mdd > -0.05 and sharpe_net > 0)
    return {
        "kill": bool(kill),
        "advance_to_paper": bool(advance and not kill and not sanity_ceiling),
        "sanity_ceiling_triggered": bool(sanity_ceiling),
        "verdict": ("LEAKAGE-AUDIT" if sanity_ceiling else
                    "KILL" if kill else
                    "ADVANCE" if advance else "ITERATE"),
        "inputs": {"rank_ic": rank_ic, "sharpe_net": sharpe_net, "mdd": mdd,
                   "beats_spy": beats_spy, "beats_mom": beats_mom},
    }
