"""Synthetic tests for the iBorrowDesk v2 all-time backfill in data_acquisition/src/fetch_borrow.py —
no network, no volume, no units spent.

The vendor is faked at _ibd2_request with the billing rules from the live OpenAPI spec (ceil of the
returned span / 365 per symbol, a 400 when a request's worst case tops the per-request ceiling, 402 on
a spent allowance, unknown symbols and empty answers unbilled). What these pin down:
  - breadth-first: a short allowance buys EVERY name the same recent depth, later allowances deepen them
  - chunks are exact 365-day multiples, so splitting costs no more than one all-time request
  - rows merge under what history/ already holds (existing days win) in the free endpoint's row shape
  - dashed class shares are asked for with a dot and stored under the dash
  - a finished name is never requested again; legacy all-time records count as finished
  - a name listed inside a chunk completes on the next (empty, unbilled) chunk
  - validation cross-checks v2 closes against the free pull and flags inconsistent OHLC
  - BORROW_V2_ONLY runs no snapshot and never writes _run.json
"""
import importlib.util
import json
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "data_acquisition" / "src" / "fetch_borrow.py"
NOW = datetime(2026, 9, 15, 4, 0, tzinfo=timezone.utc)
EARLIEST = date(2015, 7, 13)
END = date(2026, 7, 8)


@pytest.fixture
def fb(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fetch_borrow_under_test", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "log", lambda msg: None)
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    monkeypatch.setattr(mod, "IBD2_KEY", "ibd_test")
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(mod, "CONFIG_PATH", str(tmp_path / "code" / "borrow.json"))
    monkeypatch.setattr(mod, "_utcnow", lambda: NOW)
    return mod


class FakeV2:
    """`listings` maps VENDOR symbols to their first day. Observations are weekly (Mondays) to keep the
    payloads small; billing uses the returned first/last dates exactly as the vendor documents."""

    def __init__(self, fb, listings, remaining=500, ceiling=150, usage_remaining=None, fail_usage=None,
                 bad_ohlc=False):
        self.fb, self.listings, self.remaining, self.ceiling = fb, listings, remaining, ceiling
        self.usage_remaining, self.fail_usage, self.bad_ohlc = usage_remaining, fail_usage, bad_ohlc
        self.calls, self.rejected = [], 0

    def daily_calls(self):
        return [p for path, p in self.calls if path == "/daily/borrow"]

    def __call__(self, path, params=None, auth=True):
        self.calls.append((path, dict(params or {})))
        if path == "/coverage":
            return {"daily_earliest_available": EARLIEST.isoformat(), "daily_max_units_per_request": 150,
                    "max_symbols": 100, "unit_days": 365}
        if path == "/usage":
            if self.fail_usage:
                raise self.fb.V2Error(*self.fail_usage)
            left = self.remaining if self.usage_remaining is None else self.usage_remaining
            return {"allowance": {"remaining": left, "units": 500, "resets_at": "2026-10-01T00:00:00+00:00"}}
        assert path == "/daily/borrow"
        syms = params["symbols"].split(",")
        end = date.fromisoformat(params["end"])
        start = max(EARLIEST, date.fromisoformat(params.get("start") or EARLIEST.isoformat()))
        worst = math.ceil(((end - start).days + 1) / 365) * len(syms)
        if worst > self.ceiling:
            self.rejected += 1
            raise self.fb.V2Error(400, "invalid_request", "failed validation",
                                  {"_schema": [f"worst case {worst} units exceeds {self.ceiling}"]})
        data, billed, unresolved = {}, 0, []
        for s in syms:
            if s not in self.listings:
                unresolved.append(s)
                continue
            first = max(date.fromisoformat(self.listings[s]), start)
            first += timedelta(days=(7 - first.weekday()) % 7)
            last = end - timedelta(days=end.weekday())
            obs, d = [], first
            while d <= last:
                fee_close = 0.5 if self.bad_ohlc else 0.3          # 0.5 sits above the day's 0.4 high
                obs.append({"date": d.isoformat(),
                            "fee": {"open": 0.25, "high": 0.4, "low": 0.2, "close": fee_close},
                            "rebate": {"open": 4.0, "high": 4.1, "low": 3.9, "close": 4.05},
                            "available": {"open": 900000, "high": 1000000, "low": 500000, "close": 800000}})
                d += timedelta(days=7)
            units = math.ceil(((date.fromisoformat(obs[-1]["date"]) - date.fromisoformat(obs[0]["date"])).days
                               + 1) / 365) if obs else 0
            billed += units
            data[s] = {"figi": f"BBG{s}", "name": s, "country": "usa",
                       "first_date": obs[0]["date"] if obs else None, "last_date": obs[-1]["date"] if obs else None,
                       "units": units, "previous_identities": [], "observations": obs}
        if billed > self.remaining:
            raise self.fb.V2Error(402, "budget_exhausted", "This month's 500 Patreon API units are spent.")
        self.remaining -= billed
        return {"data": data, "meta": {"units_billed": billed, "unresolved": unresolved, "end": end.isoformat(),
                                       "allowance": {"remaining": self.remaining,
                                                     "resets_at": "2026-10-01T00:00:00+00:00"}}}


def write_configs(tmp_path, own, intraday_stocks, market=(), tickers=None, exclude=False):
    code = tmp_path / "code"
    code.mkdir(parents=True, exist_ok=True)
    (code / "intraday.json").write_text(json.dumps({"stocks": list(intraday_stocks), "market": list(market)}))
    (code / "tickers.json").write_text(json.dumps({"stocks": list(tickers or [])}))
    v2 = {"enabled": True, "stocks_from": "intraday.json", "stocks_from_keys": ["stocks", "market"],
          "only_in_history_universe": True, "overlap_free_window_days": 60}
    if exclude:
        v2["exclude_stocks_from"] = "tickers.json"
    return {"countries": [], "iborrowdesk": {"enabled": True, "stocks": list(own)}, "iborrowdesk_v2": v2}


def history(fb, t):
    return json.loads((Path(fb.DATA_DIR) / "history" / f"{t}.json").read_text())


def record(fb, t):
    return json.loads((Path(fb.DATA_DIR) / "history_v2" / f"{t}.json").read_text())


def test_window_stops_short_of_the_free_year(fb):
    assert fb._v2_window(EARLIEST, 365, date(2026, 9, 15), 60) == (END, 11)
    assert fb._v2_window(EARLIEST, 365, date(2026, 9, 15), None) == (date(2026, 9, 15), 12)


def test_full_allowance_completes_in_one_request_under_existing_rows(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["AAA", "BRK-B", "NEW", "ZZZ"], intraday_stocks=["AAA", "BRK-B", "NEW", "OTHER"],
                        market=["SPY"])
    hist = Path(fb.DATA_DIR) / "history"
    hist.mkdir(parents=True)
    sentinel = {"ticker": "AAA", "date": "2026-01-05", "fee_bps_yr": 999.0, "source": "iborrowdesk"}
    (hist / "AAA.json").write_text(json.dumps([sentinel]))
    fake = FakeV2(fb, {"AAA": "2010-01-04", "BRK.B": "2010-01-04", "NEW": "2024-01-08"})
    monkeypatch.setattr(fb, "_ibd2_request", fake)

    s = fb.collect_history_v2(cfg)

    assert (s["ok"], s["universe"], s["complete"], s["deferred"]) == (True, 3, 3, 0)
    assert fake.daily_calls() == [{"symbols": "AAA,BRK.B,NEW", "start": "2015-07-13", "end": "2026-07-08"}]
    assert s["units_billed"] == 11 + 11 + 3 and s["remaining"] == 500 - 25
    aaa = history(fb, "AAA")
    assert aaa[0]["date"] == "2015-07-13" and aaa[0]["fee_bps_yr"] == 30.0
    assert [r for r in aaa if r["date"] == "2026-01-05"] == [sentinel]          # existing day untouched
    assert set(aaa[0]) == {"ticker", "date", "fee_bps_yr", "rebate_rate_pct", "available", "fee_bps_yr_high",
                           "fee_bps_yr_low", "fee_bps_yr_open", "available_low", "ticker_vendor", "source"}
    assert (aaa[0]["fee_bps_yr_high"], aaa[0]["available_low"], aaa[0]["source"]) == (40.0, 500000, "iborrowdesk_v2")
    brk = history(fb, "BRK-B")
    assert brk[0]["ticker"] == "BRK-B" and brk[0]["ticker_vendor"] == "BRK.B"
    raw = record(fb, "BRK-B")
    assert raw["figi"] == "BBGBRK.B" and raw["complete"] and raw["observations"][0]["fee"]["high"] == 0.4

    fake.calls.clear()
    again = fb.collect_history_v2(cfg)
    assert fake.calls == [] and again["pending"] == 0                           # done means no request


def test_short_allowance_goes_breadth_first_then_deepens(fb, tmp_path, monkeypatch):
    names = [f"S{i:02d}" for i in range(30)]
    cfg = write_configs(tmp_path, own=names, intraday_stocks=names)
    fake = FakeV2(fb, {n: "2001-01-02" for n in names}, remaining=150)
    monkeypatch.setattr(fb, "_ibd2_request", fake)

    first = fb.collect_history_v2(cfg)
    assert (first["ok"], first["symbols_fetched"], first["complete"], first["remaining"]) == (True, 30, 0, 0)
    assert len(first["partial"]) == 30 and fake.remaining == 0                  # never asked past the allowance
    starts = {record(fb, n)["first_date"] for n in names}
    assert len(starts) == 1 and date.fromisoformat(starts.pop()) >= END - timedelta(days=5 * 365)

    fake.remaining = 500                                                        # the 1st: allowance resets
    second = fb.collect_history_v2(cfg)
    assert (second["complete"], second["deferred"]) == (30, 0)
    total = sum(record(fb, n)["units"] for n in names)
    assert total == 30 * 11                                                     # same as one all-time ask each
    assert all(len(record(fb, n)["chunks"]) == 2 for n in names) and len(fake.daily_calls()) == 3
    assert all(history(fb, n)[0]["date"] == "2015-07-13" for n in names)
    days = [r["date"] for r in history(fb, "S00")]
    assert len(days) == len(set(days)) and days == sorted(days)                 # chunk seam: no dup, no hole
    assert all((date.fromisoformat(b) - date.fromisoformat(a)).days == 7 for a, b in zip(days, days[1:]))

    fake.calls.clear()
    assert fb.collect_history_v2(cfg)["pending"] == 0 and fake.calls == []


def test_listing_inside_a_chunk_completes_on_the_empty_chunk(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["NEW"], intraday_stocks=["NEW"])
    fake = FakeV2(fb, {"NEW": "2024-01-08"}, remaining=4)
    monkeypatch.setattr(fb, "_ibd2_request", fake)
    s = fb.collect_history_v2(cfg)
    rec = record(fb, "NEW")
    assert rec["complete"] and rec["first_date"] == "2024-01-08" and rec["units"] == 3
    assert (s["units_billed"], s["remaining"], s["requests"]) == (3, 1, 2)       # 2nd chunk empty and free


def test_legacy_all_time_record_counts_as_complete(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["OLD"], intraday_stocks=["OLD"])
    legacy = {"ticker": "OLD", "window": {"start": None, "end": "2026-07-08"}, "units": 11,
              "observations": [{"date": "2015-07-13", "fee": {}, "rebate": {}, "available": {}}]}
    (Path(fb.DATA_DIR) / "history_v2").mkdir(parents=True)
    (Path(fb.DATA_DIR) / "history_v2" / "OLD.json").write_text(json.dumps(legacy))
    fake = FakeV2(fb, {"OLD": "2010-01-04"})
    monkeypatch.setattr(fb, "_ibd2_request", fake)
    s = fb.collect_history_v2(cfg)
    assert fake.calls == [] and s["complete"] == 1


def test_stale_usage_402_defers_without_failing(fb, tmp_path, monkeypatch):
    names = ["AAA", "BBB"]
    cfg = write_configs(tmp_path, own=names, intraday_stocks=names)
    fake = FakeV2(fb, {n: "2010-01-04" for n in names}, remaining=5, usage_remaining=500)
    monkeypatch.setattr(fb, "_ibd2_request", fake)
    s = fb.collect_history_v2(cfg)
    assert (s["ok"], s["complete"], s["deferred"], s["remaining"]) == (True, 0, 2, 0)


def test_cost_rejection_shrinks_the_batch(fb, tmp_path, monkeypatch):
    names = [f"S{i:02d}" for i in range(20)]
    cfg = write_configs(tmp_path, own=names, intraday_stocks=names)
    fake = FakeV2(fb, {n: "2010-01-04" for n in names}, ceiling=100)
    monkeypatch.setattr(fb, "_ibd2_request", fake)
    s = fb.collect_history_v2(cfg)
    assert (s["ok"], s["complete"], s["deferred"]) == (True, 20, 0)
    assert fake.rejected == 1 and all(len(c["symbols"].split(",")) <= 6 for c in fake.daily_calls()[1:])


def test_unresolved_is_unbilled_and_retried_after_30_days(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["AAA", "GONE"], intraday_stocks=["AAA", "GONE"])
    fake = FakeV2(fb, {"AAA": "2010-01-04"})
    monkeypatch.setattr(fb, "_ibd2_request", fake)
    s = fb.collect_history_v2(cfg)
    assert (s["complete"], s["unresolved"], s["units_billed"]) == (1, ["GONE"], 11)

    fake.calls.clear()
    assert fb.collect_history_v2(cfg)["pending"] == 0 and fake.calls == []
    monkeypatch.setattr(fb, "_utcnow", lambda: NOW + timedelta(days=31))
    assert fb.collect_history_v2(cfg)["pending"] == 1
    assert fake.daily_calls()[-1]["symbols"] == "GONE"


def test_watchlist_pass_skips_names_the_main_pass_owns(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["BRK-B", "TSM", "SPY"], intraday_stocks=["BRK-B", "TSM", "MU"],
                        market=["SPY"], tickers=["BRK-B", "MU"], exclude=True)
    fake = FakeV2(fb, {"BRK.B": "2010-01-04", "TSM": "2010-01-04", "SPY": "2010-01-04"})
    monkeypatch.setattr(fb, "_ibd2_request", fake)
    s = fb.collect_history_v2(cfg)
    assert s["universe"] == 2 and fake.daily_calls()[0]["symbols"] == "TSM,SPY"


def test_validation_cross_checks_the_free_pull(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["AAA", "BBB"], intraday_stocks=["AAA", "BBB"])
    hist = Path(fb.DATA_DIR) / "history"
    hist.mkdir(parents=True)
    agree = {"ticker": "AAA", "date": "2026-01-05", "fee_bps_yr": 30.0, "available": 800000, "source": "iborrowdesk"}
    (hist / "AAA.json").write_text(json.dumps([agree]))
    disagree = [{"ticker": "BBB", "date": f"2026-0{m}-0{d}", "fee_bps_yr": 75.0, "available": 800000,
                 "source": "iborrowdesk"} for m, d in ((1, 5), (2, 2), (3, 2))]
    (hist / "BBB.json").write_text(json.dumps(disagree))
    monkeypatch.setattr(fb, "_ibd2_request", FakeV2(fb, {"AAA": "2010-01-04", "BBB": "2010-01-04"}))

    v = fb.collect_history_v2(cfg)["validation"]["totals"]
    report = json.loads((Path(fb.DATA_DIR) / "history_v2" / "_validation.json").read_text())["names"]
    assert report["AAA"]["overlap_days"] == 1 and report["AAA"]["ok"]
    assert report["BBB"]["fee_mismatch"] == 3 and not report["BBB"]["ok"]
    assert v["failing"] == ["BBB"] and v["complete"] == 2


def test_validation_flags_inconsistent_ohlc(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["AAA"], intraday_stocks=["AAA"])
    monkeypatch.setattr(fb, "_ibd2_request", FakeV2(fb, {"AAA": "2025-01-06"}, bad_ohlc=True))
    v = fb.collect_history_v2(cfg)["validation"]["totals"]
    assert v["failing"] == ["AAA"]


def test_v2_only_takes_no_snapshot_and_leaves_run_json_alone(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["AAA"], intraday_stocks=["AAA"])
    Path(fb.CONFIG_PATH).write_text(json.dumps({**cfg, "countries": ["usa"]}))
    monkeypatch.setattr(fb, "_ibd2_request", FakeV2(fb, {"AAA": "2010-01-04"}))
    monkeypatch.setattr(fb, "fetch_file", lambda *a: pytest.fail("snapshot must not run"))
    monkeypatch.setattr(fb, "collect_history", lambda *a: pytest.fail("free pull must not run"))
    monkeypatch.setattr(fb, "V2_ONLY", True)
    assert fb.main() == 0
    assert (Path(fb.DATA_DIR) / "_run_history_v2.json").exists()
    assert not (Path(fb.DATA_DIR) / "_run.json").exists()


def test_missing_key_and_bad_key_report_without_raising(fb, tmp_path, monkeypatch):
    cfg = write_configs(tmp_path, own=["AAA"], intraday_stocks=["AAA"])
    fake = FakeV2(fb, {"AAA": "2010-01-04"}, fail_usage=(401, "invalid_api_key", "Invalid or missing API key."))
    monkeypatch.setattr(fb, "_ibd2_request", fake)

    monkeypatch.setattr(fb, "IBD2_KEY", "")
    skipped = fb.collect_history_v2(cfg)
    assert skipped["ok"] and "not set" in skipped["error"] and fake.calls == []

    monkeypatch.setattr(fb, "IBD2_KEY", "ibd_revoked")
    failed = fb.collect_history_v2(cfg)
    assert not failed["ok"] and "invalid_api_key" in failed["error"] and fake.daily_calls() == []
