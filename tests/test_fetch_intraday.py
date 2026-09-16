"""Synthetic tests for data_acquisition/src/fetch_intraday.py — no network, no volume, no credits.

The vendor is faked at fetch_window (the CSV body still goes through the real parser), the clock is
pinned via _now, and the RunPod volume API is faked inside Space. What these pin down:
  - append-only: an existing bar is never replaced, a year file with nothing new is not rewritten
  - Eastern offsets / ET-year partitioning, including 19:xx EST bars that land on the next UTC day
  - cold backfill -> warm top-up adds only the new sessions
  - a missing historical session is re-asked once; a vendor hole is not re-billed on later runs
  - a symbol the vendor has no intraday for costs one probe, not a 22-year walk
  - space: grows 1 GB at a time until the floor is met, and stops at the per-run cap
"""
import importlib.util
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq  # noqa: E402

SRC = Path(__file__).resolve().parent.parent / "data_acquisition" / "src" / "fetch_intraday.py"


@pytest.fixture
def fi(monkeypatch):
    spec = importlib.util.spec_from_file_location("fetch_intraday_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "log", lambda msg: None)
    return mod


def utc(y, m, d, hh=0, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp())


def sessions_between(a, b, holes=()):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5 and d.isoformat() not in holes:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


BAR_MINUTES = (240, 570, 571, 959, 1199)          # 04:00, 09:30, 09:31, 15:59, 19:59 ET


class FakeVendor:
    """Serves BAR_MINUTES bars for every weekday session as vendor CSV. `version` shifts prices so a
    re-pull that replaced existing bars would be visible. `missing_once` sessions are absent on the
    first request that covers them; `holes` are never served."""

    def __init__(self, fi, holes=(), missing_once=(), empty=False):
        self.fi, self.holes, self.missing_once, self.empty = fi, set(holes), set(missing_once), empty
        self.version, self.requests = 0, []

    def __call__(self, cfg, symbol, ts_from, ts_to):
        self.requests.append((symbol, int(ts_from), int(ts_to)))
        lines = ["Timestamp,Gmtoffset,Datetime,Open,High,Low,Close,Volume"]
        if not self.empty:
            d = datetime.fromtimestamp(ts_from, tz=timezone.utc).date() - timedelta(days=1)
            end = datetime.fromtimestamp(ts_to, tz=timezone.utc).date() + timedelta(days=1)
            served = set()
            while d <= end:
                iso = d.isoformat()
                if d.weekday() < 5 and iso not in self.holes:
                    if iso in self.missing_once:
                        served.add(iso)
                    else:
                        n = (d - date(1970, 1, 1)).days
                        off = self.fi.et_offset_for_utc_day(n)
                        for m in BAR_MINUTES:
                            ts = n * 86400 + m * 60 - off
                            if ts_from <= ts <= ts_to:
                                px = 100 + (n % 13) + m / 1e4 + self.version
                                lines.append(f'{ts},0,"x",{px},{px + 1},{px - 1},{px + 0.5},{m}')
                d += timedelta(days=1)
            self.missing_once -= served
        return self.fi.parse_csv(("\n".join(lines) + "\n").encode())


def make_ctx(fi, root, cfg, sessions):
    store = fi.Store(str(root))
    space = fi.Space(str(root), 0.0, 1, 5)
    return fi.Ctx(cfg, str(root), store, fi.Budget(100000, 0), space, 1e18, sessions)


CFG = {"from": "2025-10-01", "window_days": 30, "resettle_days": 4, "workers": 2, "gap_fill": True,
       "no_data_retry_days": 7, "verify_sessions": 3}


# ---------------------------------------------------------------- parser + offsets
def test_parse_csv_offsets_nulls_and_header_only(fi):
    assert fi.parse_csv(b"Timestamp,Gmtoffset,Datetime,Open,High,Low,Close,Volume\n").num_rows == 0
    assert fi.parse_csv(b"").num_rows == 0
    summer = utc(2026, 7, 1, 13, 30)         # 09:30 EDT
    winter = utc(2026, 1, 5, 14, 30)         # 09:30 EST
    body = (b"Timestamp,Gmtoffset,Datetime,Open,High,Low,Close,Volume\n"
            + f'{summer},0,"a",10,,,11,\n'.encode()
            + f'{winter},0,"b",20,21,19,20.5,300\n'.encode()
            + f'{winter + 60},0,"c",,21,19,20.5,300\n'.encode())       # no open -> dropped
    t = fi.parse_csv(body)
    assert t.schema == fi.schema()
    rows = t.to_pylist()
    assert len(rows) == 2
    assert rows[0] == {"ts": summer, "gmtoffset": -14400, "open": 10.0, "high": 10.0, "low": 10.0, "close": 11.0, "volume": 0.0}
    assert rows[1]["gmtoffset"] == -18000 and rows[1]["volume"] == 300.0
    with pytest.raises(RuntimeError):
        fi.parse_csv(b"Ticker Not Found.")


def test_after_hours_on_new_years_eve_stays_in_its_et_year(fi):
    ts = utc(2026, 1, 1, 0, 30)               # 2025-12-31 19:30 EST
    body = f"Timestamp,Gmtoffset,Datetime,Open,High,Low,Close,Volume\n{ts},0,\"x\",1,1,1,1,1\n".encode()
    parts = fi.split_by_year(fi.parse_csv(body))
    assert list(parts) == [2025]


# ---------------------------------------------------------------- append-only merge
def test_merge_year_never_replaces_existing_bars_and_skips_untouched_files(fi, tmp_path):
    path = str(tmp_path / "1m" / "X" / "2026.parquet")
    base = pa.table({"ts": pa.array([100, 200, 300], pa.int64()), "gmtoffset": pa.array([-14400] * 3, pa.int32()),
                     "open": [1.0, 2.0, 3.0], "high": [1.0, 2.0, 3.0], "low": [1.0, 2.0, 3.0], "close": [1.0, 2.0, 3.0],
                     "volume": [1.0, 1.0, 1.0]}, schema=fi.schema())
    assert fi.merge_year(path, base) == 3
    before = (os.stat(path).st_mtime_ns, Path(path).read_bytes())

    restated = base.set_column(2, "open", pa.array([9.0, 9.0, 9.0]))            # vendor "changed" old bars
    assert fi.merge_year(path, restated) == 0
    assert (os.stat(path).st_mtime_ns, Path(path).read_bytes()) == before          # not rewritten

    more = pa.table({"ts": pa.array([200, 250, 400, 400], pa.int64()), "gmtoffset": pa.array([-14400] * 4, pa.int32()),
                     "open": [9.0, 2.5, 4.0, 7.0], "high": [9.0, 2.5, 4.0, 7.0], "low": [9.0, 2.5, 4.0, 7.0],
                     "close": [9.0, 2.5, 4.0, 7.0], "volume": [1.0] * 4}, schema=fi.schema())
    assert fi.merge_year(path, more) == 2
    t = pq.read_table(path)
    assert t["ts"].to_pylist() == [100, 200, 250, 300, 400]
    assert t["open"].to_pylist() == [1.0, 2.0, 2.5, 3.0, 4.0]       # 200 kept its bar; first 400 wins


# ---------------------------------------------------------------- per-symbol flow
def test_cold_backfill_then_warm_topup_adds_only_new_sessions(fi, tmp_path, monkeypatch):
    sessions = sessions_between(date(2025, 9, 1), date(2026, 3, 31))
    vendor = FakeVendor(fi)
    monkeypatch.setattr(fi, "fetch_window", vendor)
    monkeypatch.setattr(fi, "DAILY_DIR", str(tmp_path / "daily"))
    monkeypatch.setattr(fi, "_now", lambda: utc(2026, 2, 10, 12))
    ctx = make_ctx(fi, tmp_path, CFG, sessions)

    r = fi.work_symbol(ctx, "AAA")
    assert r["ok"] and r["complete"], r
    y2025 = tmp_path / "1m" / "AAA" / "2025.parquet"
    assert sorted(os.listdir(tmp_path / "1m" / "AAA")) == ["2025.parquet", "2026.parquet"]
    snap = (os.stat(y2025).st_mtime_ns, y2025.read_bytes())
    rows_before = ctx.store.entry("AAA")["rows"]
    last_before = ctx.store.entry("AAA")["last_ts"]

    vendor.version = 5                                       # vendor now answers with different prices
    vendor.requests.clear()
    monkeypatch.setattr(fi, "_now", lambda: utc(2026, 2, 20, 12))
    ctx2 = make_ctx(fi, tmp_path, CFG, sessions)
    r2 = fi.work_symbol(ctx2, "AAA")
    assert r2["ok"] and r2["complete"]
    assert len(vendor.requests) == 1                          # one tail window, no backfill, no gaps
    assert (os.stat(y2025).st_mtime_ns, y2025.read_bytes()) == snap   # closed year untouched
    new_sessions = len(sessions_between(date(2026, 2, 11), date(2026, 2, 20)))
    assert r2["added"] == new_sessions * len(BAR_MINUTES)
    assert ctx2.store.entry("AAA")["rows"] == rows_before + r2["added"]
    assert ctx2.store.entry("AAA")["last_ts"] > last_before
    t = pq.read_table(tmp_path / "1m" / "AAA" / "2026.parquet")
    first_day = [o for ts, o in zip(t["ts"].to_pylist(), t["open"].to_pylist()) if ts < utc(2026, 2, 6)]
    assert first_day and all(o < 105 + 13 for o in first_day)       # pre-existing bars kept version-0 prices


def test_missing_session_is_reasked_once_and_vendor_holes_are_not_rebilled(fi, tmp_path, monkeypatch):
    sessions = sessions_between(date(2025, 9, 1), date(2026, 3, 31))
    vendor = FakeVendor(fi, holes={"2025-11-12"}, missing_once={"2025-12-03"})
    monkeypatch.setattr(fi, "fetch_window", vendor)
    monkeypatch.setattr(fi, "DAILY_DIR", str(tmp_path / "daily"))
    monkeypatch.setattr(fi, "_now", lambda: utc(2026, 2, 10, 12))
    ctx = make_ctx(fi, tmp_path, CFG, sessions)
    r = fi.work_symbol(ctx, "BBB")
    assert r["ok"] and r.get("gap_windows") == 1

    a = ctx.store.load_audit("BBB")
    present = set()
    for f in fi.audit_symbol(ctx.store, str(tmp_path), "BBB")["files"].values():
        present.update(f["days"])
    assert fi.ord_day("2025-12-03") in present                 # filled by the gap pass
    assert fi.ord_day("2025-11-12") not in present             # vendor hole stays a hole
    assert fi.ord_day("2025-11-12") in a["tried_days"]

    vendor.requests.clear()
    monkeypatch.setattr(fi, "_now", lambda: utc(2026, 2, 12, 12))
    ctx2 = make_ctx(fi, tmp_path, CFG, sessions)
    r2 = fi.work_symbol(ctx2, "BBB")
    assert r2["ok"] and not r2.get("gap_windows")
    assert len(vendor.requests) == 1                           # tail only; the hole is not re-billed

    v = fi.verify(ctx2, ["BBB"])["symbols"]["BBB"]
    assert v["missing_sessions_old"] == 1 and v["missing_old_untried"] == 0
    assert v["structure"] == {"unsorted": 0, "dup_ts": 0, "bad_offset": 0, "wrong_year": 0, "out_of_hours": 0, "bad_ohlc": 0}


def test_symbol_without_vendor_intraday_costs_one_probe(fi, tmp_path, monkeypatch):
    vendor = FakeVendor(fi, empty=True)
    monkeypatch.setattr(fi, "fetch_window", vendor)
    monkeypatch.setattr(fi, "_now", lambda: utc(2026, 2, 10, 12))
    ctx = make_ctx(fi, tmp_path, CFG, sessions_between(date(2025, 9, 1), date(2026, 3, 31)))
    r = fi.work_symbol(ctx, "BF-B")
    assert r["ok"] and r["requests"] == 1 and "no vendor data" in r["note"]
    assert not (tmp_path / "1m" / "BF-B").exists()
    r2 = fi.work_symbol(make_ctx(fi, tmp_path, CFG, []), "BF-B")
    assert r2["requests"] == 0 and len(vendor.requests) == 1


def test_verify_passes_on_a_clean_store_and_flags_unpulled_symbols(fi, tmp_path, monkeypatch):
    sessions = sessions_between(date(2025, 9, 1), date(2026, 3, 31))
    vendor = FakeVendor(fi)
    monkeypatch.setattr(fi, "fetch_window", vendor)
    now = utc(2026, 2, 10, 12)
    monkeypatch.setattr(fi, "_now", lambda: now)
    daily = tmp_path / "daily"; daily.mkdir()
    monkeypatch.setattr(fi, "DAILY_DIR", str(daily))
    ctx = make_ctx(fi, tmp_path / "store", CFG, sessions)
    (tmp_path / "store").mkdir(exist_ok=True)
    assert fi.work_symbol(ctx, "CCC")["complete"]
    opens = []
    for d in sessions_between(date(2026, 2, 2), date(2026, 2, 9)):
        n = fi.ord_day(d)
        opens.append({"date": d, "open": 100 + (n % 13) + 570 / 1e4})
    (daily / "CCC.json").write_text(json.dumps(opens))

    s = fi.verify(ctx, ["CCC"])["summary"]
    assert s["status"] == "verified", s
    assert s["open_checks"] == 3 and s["open_check_worst_bps"] == 0
    assert s["symbols_with_extended_hours_on_last_session"] == 1
    s2 = fi.verify(ctx, ["CCC", "DDD"])["summary"]
    assert s2["status"] == "incomplete" and s2["symbols_not_pulled"] == ["DDD"]


# ---------------------------------------------------------------- universe + space
def test_universe_is_the_union_of_configs_research_names_first(fi, tmp_path):
    (tmp_path / "tickers.json").write_text(json.dumps({"stocks": ["AAPL", "MMM", "BF-B"], "market": ["SPY.US"]}))
    (tmp_path / "watch.json").write_text(json.dumps({"stocks": ["TSM", "MSTR"], "market": ["SPY.US", "QQQ.US"]}))
    cfg = {"universe_configs": ["tickers.json", "watch.json"], "stocks": ["NVDA", "AAPL"], "market": ["SPY", "QQQ"]}
    assert fi.resolve_universe(cfg, str(tmp_path)) == ["NVDA", "AAPL", "MMM", "BF-B", "TSM", "MSTR", "SPY", "QQQ"]


def test_space_grows_one_gb_at_a_time_until_the_floor_and_respects_the_cap(fi, tmp_path, monkeypatch):
    vol = {"size": 10, "used": 9.5, "patches": []}

    def fake_api(self, method="GET", size=None):
        if method == "PATCH":
            vol["patches"].append(size); vol["size"] = size
        return {"size": vol["size"]}

    monkeypatch.setattr(fi.Space, "_api", fake_api)
    monkeypatch.setattr(fi.Space, "_statvfs", lambda self: (float(vol["size"]), vol["size"] - vol["used"]))
    monkeypatch.setattr(fi.time, "sleep", lambda s: None)
    monkeypatch.setattr(fi, "VOLUME_ID", "vol"); monkeypatch.setattr(fi, "RUNPOD_KEY", "key")

    sp = fi.Space(str(tmp_path), 3.0, 1, 30)
    assert sp.mode == "statvfs"
    assert sp.ensure() is True
    assert vol["patches"] == [11, 12, 13] and sp.grown == 3 and sp.free_gb() >= 3.0

    vol.update(size=10, used=9.5, patches=[])
    capped = fi.Space(str(tmp_path), 3.0, 1, 2)
    assert capped.ensure() is False and vol["patches"] == [11, 12]


def test_space_without_volume_api_does_not_grow(fi, tmp_path, monkeypatch):
    monkeypatch.setattr(fi, "VOLUME_ID", ""); monkeypatch.setattr(fi.Space, "_statvfs", lambda self: (100.0, 0.5))
    sp = fi.Space(str(tmp_path), 3.0, 1, 30)
    assert sp.mode == "statvfs-only" and sp.ensure() is False


# ---------------------------------------------------------------- orchestration
def test_main_runs_tails_then_gaps_writes_run_and_verify(fi, tmp_path, monkeypatch):
    """main() end to end: two symbols with data, one the vendor lacks; exit 0, _run.json and
    _verify.json written, gap pass after all tails."""
    conf = tmp_path / "code"; conf.mkdir()
    (conf / "tickers.json").write_text(json.dumps({"stocks": ["AAA", "BF-B"], "market": ["SPY.US"]}))
    cfg = dict(CFG, universe_configs=["tickers.json"], stocks=["AAA"], market=[], reserve_credits=0,
               max_requests_per_run=1000, min_free_gb=0.0, max_run_minutes=5)
    (conf / "intraday.json").write_text(json.dumps(cfg))
    sessions = sessions_between(date(2025, 9, 1), date(2026, 3, 31))
    (tmp_path / "XNYS.json").write_text(json.dumps({"sessions": sessions}))
    real_vendor = FakeVendor(fi, holes={"2025-11-12"})
    empty_vendor = FakeVendor(fi, empty=True)
    calls = []

    def vendor(c, symbol, a, b):
        calls.append(symbol)
        return (empty_vendor if symbol == "BF-B" else real_vendor)(c, symbol, a, b)

    for k, v in (("DATA_DIR", str(tmp_path / "vol" / "data" / "tickdata")), ("CONFIG_PATH", str(conf / "intraday.json")),
                 ("TOKEN", "t"), ("SESSIONS_PATH", str(tmp_path / "XNYS.json")), ("DAILY_DIR", str(tmp_path / "daily")),
                 ("VOLUME_ID", ""), ("fetch_window", vendor), ("credit_status", lambda: (0, 100000)),
                 ("_now", lambda: utc(2026, 2, 10, 12))):
        monkeypatch.setattr(fi, k, v)
    assert fi.main() == 0
    root = tmp_path / "vol" / "data" / "tickdata"
    run = json.loads((root / "_run.json").read_text())
    assert run["n_configured"] == 3 and run["n_fail"] == 0 and run["stop_reason"] is None
    by = {r["symbol"]: r for r in run["results"]}
    assert set(by) == {"AAA", "BF-B", "SPY"} and by["BF-B"]["requests"] == 1
    assert by["AAA"]["gap_windows"] == 1
    ver = json.loads((root / "_verify.json").read_text())["summary"]
    assert ver["symbols_no_vendor_data"] == ["BF-B"] and ver["symbols_with_data"] == 2
    assert ver["missing_sessions_old"] == 2 and ver["missing_sessions_old_untried"] == 0
    # second run the same day: tails only (1 request per held symbol), BF-B not re-probed, no gap requests
    calls.clear()
    assert fi.main() == 0
    assert sorted(calls) == ["AAA", "SPY"]
