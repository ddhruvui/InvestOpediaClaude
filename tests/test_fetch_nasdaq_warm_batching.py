"""Warm per-ticker pulls must filter server-side by ticker batch.

Regression for 2026-09-16: the 506-name universe fell back to an UNFILTERED whole-market
`<incr_col>.gte=` walk. Sharadar caps skip/offset at 500,000, so SEP (whole-market DAILY
prices since a drifted watermark) died at page 50 with HTTP 400 "skip too large" and the
whole nasdaq job exited fetch=1.
"""
import importlib.util
import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "data_acquisition", "src", "fetch_nasdaq.py")


def _load():
    spec = importlib.util.spec_from_file_location("fetch_nasdaq_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


fn = _load()

# Offset cap Sharadar enforces; a walk that needs a page at or past this 400s.
SKIP_CAP = 500_000


def test_ticker_batches_respect_both_server_caps():
    syms = [f"TICK{i:04d}" for i in range(506)]
    batches = list(fn._ticker_batches(syms))
    assert sum(len(b) for b in batches) == len(syms)
    assert [s for b in batches for s in b] == syms          # order + no drops
    for b in batches:
        assert len(b) <= fn.MAX_TICKERS_PER_CALL
        assert len(",".join(b)) <= fn.MAX_TICKER_CHARS


def test_warm_pull_is_ticker_filtered_and_never_trips_the_skip_cap(monkeypatch):
    """Replay the warm branch's batch loop (fetch_series is nested in main(), so this drives
    _ticker_batches + _fetch in the same shape) against a stub that emulates the live 400."""
    syms = [f"TICK{i:04d}" for i in range(506)]
    calls = []

    # Whole-market daily prices since the watermark: far more than the offset cap.
    WHOLE_MARKET_ROWS = 3_000_000

    def fake_get(endpoint, params):
        calls.append(dict(params))
        offset = int(params.get("offset", 0))
        if offset >= SKIP_CAP:
            raise RuntimeError(
                f"unexpected response (status=400) from /{endpoint} — "
                '{"error":"skip too large","description":"skip/offset cannot exceed 500,000."}'
            )
        tick = params.get("ticker")
        if not tick:
            # unfiltered whole-market walk — the broken path
            remaining = WHOLE_MARKET_ROWS - offset
        else:
            # server-side filtered: 30 tickers * ~80 sessions
            remaining = 80 * len(tick.split(",")) - offset
        n = max(0, min(fn.ROW_CAP, remaining))
        names = (tick.split(",") if tick else syms)
        return {"data": [{"ticker": names[i % len(names)], "date": "2026-09-15",
                          "lastupdated": "2026-09-15", "close": 1.0, "_i": offset + i}
                         for i in range(n)]}

    monkeypatch.setattr(fn, "_get", fake_get)

    batches = list(fn._ticker_batches(syms))
    rows = []
    for n, batch in enumerate(batches, start=1):
        base = {"lastupdated.gte": "2026-05-29", "ticker": ",".join(batch)}
        part, _ = fn._fetch("stocks", base, label=f"incr batch {n}")
        rows.extend(part)

    assert calls, "no API calls were made"
    # 1. every call carried a server-side ticker filter
    assert all(c.get("ticker") for c in calls), "an unfiltered whole-market call was issued"
    # 2. no call ever reached the offset cap
    assert max(int(c.get("offset", 0)) for c in calls) < SKIP_CAP
    # 3. one call per batch (each batch fits inside a single ROW_CAP page)
    assert len(calls) == len(batches)
    assert rows, "warm pull returned no rows"


def test_unfiltered_whole_market_walk_would_still_fail(monkeypatch):
    """Guard the guard: the stub really does reproduce the live 400 on the old path."""
    def fake_get(endpoint, params):
        offset = int(params.get("offset", 0))
        if offset >= SKIP_CAP:
            raise RuntimeError('status=400 "skip too large"')
        return {"data": [{"ticker": "X", "_i": offset + i} for i in range(fn.ROW_CAP)]}

    monkeypatch.setattr(fn, "_get", fake_get)
    with pytest.raises(RuntimeError, match="skip too large"):
        fn._fetch("stocks", {"lastupdated.gte": "2026-05-29"}, label="unfiltered")
