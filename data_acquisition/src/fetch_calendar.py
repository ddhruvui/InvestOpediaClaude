#!/usr/bin/env python3
"""D-11 trading calendar (Q-001) -> the SOURCE OF TRUTH for NYSE sessions, past and FUTURE.

Spec v1.2 §2 D-11: `exchange_calendars.get_calendar("XNYS")` -> sessions incl. half-days, past AND
future (the future half is required to schedule the t+h+1 vertical MOO — no price feed can supply
it). EODHD's exchange-details holiday snapshot (data/calendar/US.json) is only the CROSS-CHECK.

Why this matters beyond scheduling — two live defects it closes, both measured on this volume:
  * data/eod_bulk/US/ holds a day-file for every WEEKDAY the fetcher probed, and EODHD answers on
    market holidays with OTC/foreign rows (2025-09-01 Labor Day: 4,418 rows; 2026-07-03: 3,182).
    Ten such non-session files are on the volume today. Unioned naively they invent ten phantom
    sessions. `sessions` here is the mask that removes them.
  * Q-004's missing-bars check needs an authoritative session list to diff prices against; without
    it, "ticker is missing a bar" and "that day was never a session" are indistinguishable.

This is the ONE fetcher that is not stdlib-only: it needs the `exchange_calendars` pip package
(bootstrap.sh installs it when launch.sh sets PIP_PACKAGES). Zero vendor credits, no auth.

OUTPUT:
    DATA_DIR/<CALENDAR>.json   {calendar, tz, package_version, generated_at_utc, first_session,
                                last_session, sessions: [...], early_closes: {date: close_time},
                                session_open_close: {date: [open_utc, close_utc]}}

The package VERSION is recorded in the payload: exchange_calendars ships rule changes (a newly
announced holiday, a corrected historical session) in point releases, so a calendar is only
reproducible alongside the version that produced it — spec §2 D-11 "pin package version; refresh
on upgrade", and it feeds G-10's config_hash.

    DATA_DIR=./data_calendar CONFIG_PATH=data_acquisition/config/calendar.json \
      python3 data_acquisition/src/fetch_calendar.py
"""
import json
import os
import sys
import traceback
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_calendar")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/calendar.json")
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

_LOG_LINES = []


def log(msg):
    print(msg, flush=True)
    _LOG_LINES.append(msg)


def _persist_log(kind, manifest=None):
    log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(log_dir, f"{kind}-{stamp}.log")
    with open(path, "w") as f:
        if _LOG_LINES:
            f.write("\n".join(_LOG_LINES) + "\n\n")
        if manifest is not None:
            f.write("--- manifest ---\n" + json.dumps(manifest, indent=2) + "\n")
    return path


def _write(out_path, data):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".part"
    with open(tmp, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)


def build(name, start, end):
    import exchange_calendars as xcals
    cal = xcals.get_calendar(name, start=start, end=end)
    version = getattr(xcals, "__version__", "unknown")

    sessions = [d.strftime("%Y-%m-%d") for d in cal.sessions]
    # Half-days matter: the spec's cutoff/MOO timing and any intraday proxy must not assume 16:00 ET.
    opens = cal.opens
    closes = cal.closes
    normal = {}
    for d in cal.sessions:
        try:
            normal[d.strftime("%Y-%m-%d")] = [
                opens[d].tz_convert("UTC").strftime("%H:%M"),
                closes[d].tz_convert("UTC").strftime("%H:%M"),
            ]
        except Exception:
            continue
    # An early close is any session whose close is earlier than that calendar's modal close.
    modal = None
    if normal:
        counts = {}
        for _, (_, c) in normal.items():
            counts[c] = counts.get(c, 0) + 1
        modal = max(counts, key=counts.get)
    early = {d: v[1] for d, v in normal.items() if modal and v[1] < modal}

    return {
        "calendar": name,
        "tz": str(getattr(cal, "tz", "")),
        "package": "exchange_calendars",
        "package_version": version,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "spec_item": "D-11",
        "requested_start": start,
        "requested_end": end,
        "first_session": sessions[0] if sessions else None,
        "last_session": sessions[-1] if sessions else None,
        "n_sessions": len(sessions),
        "modal_close_utc": modal,
        "sessions": sessions,
        "early_closes": early,
        "session_open_close": normal,
    }


def main():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    calendars = cfg.get("calendars") or ["XNYS"]
    start = cfg.get("start", "1999-01-01")
    # Default well into the future: the t+h+1 MOO schedule and any forward barrier need sessions
    # that do not exist yet. Regenerating is free, so over-reaching costs nothing.
    end = cfg.get("end", f"{datetime.now(timezone.utc).year + 2}-12-31")

    results = []
    for name in calendars:
        entry = {"symbol": name, "dataset": "calendar", "ok": False, "count": 0,
                 "added": 0, "error": None}
        try:
            payload = build(name, start, end)
            out = os.path.join(DATA_DIR, f"{name}.json")
            _write(out, payload)
            entry.update(ok=True, count=payload["n_sessions"], added=payload["n_sessions"])
            log(f"OK   calendar {name}: {payload['n_sessions']} sessions "
                f"{payload['first_session']}..{payload['last_session']} "
                f"({len(payload['early_closes'])} early closes, "
                f"exchange_calendars {payload['package_version']}) -> {out}")
        except ImportError as e:
            entry["error"] = (f"ImportError: {e} — this fetcher needs the `exchange_calendars` "
                              f"package (launch.sh sets PIP_PACKAGES for it; locally: "
                              f"pip install exchange_calendars)")
            log(f"FAIL calendar {name}: {entry['error']}")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"FAIL calendar {name}: {entry['error']}")
        results.append(entry)

    all_ok = bool(results) and all(r["ok"] for r in results)
    manifest = {
        "vendor": "exchange_calendars (pip, free)",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "spec_items": ["D-11", "Q-001"],
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "calendars": calendars,
        "start": start,
        "end": end,
        "ok": all_ok,
        "results": results,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    _write(os.path.join(DATA_DIR, "_run.json"), manifest)

    if not all_ok:
        log(f"FAILED — error log: {_persist_log('error', manifest)}")
        return 1
    if STORE_LOGS:
        log(f"run log: {_persist_log('run', manifest)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            _LOG_LINES.append(traceback.format_exc())
            _persist_log("crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
