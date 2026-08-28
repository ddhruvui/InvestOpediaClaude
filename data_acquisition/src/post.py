#!/usr/bin/env python3
"""Post-fetch stage: wait for today's vendor runs, then validate -> build M1.

`launch.sh all` fires every fetcher in parallel, so the two stages that CONSUME their output —
validate.py (D-12/M1-04 + Q-004 + repair) and build_m1.py (the §3/§4 landing layer) — cannot be
part of it. Running them by hand afterwards works but is exactly the sort of manual step that gets
forgotten, and a forgotten m1 means models silently read yesterday's tables.

This job closes that loop: launch it at the same time as `all` and it BLOCKS until the vendor
manifests are dated today, then runs the two stages in order in a single pod.

    launch.sh all && launch.sh post      # post waits for all to finish, then validates + builds

WAITING. Polls `<tree>/_run.json` for each vendor in WAIT_FOR until every one's `ended_at` is
NEWER than this job's launch (POST_LAUNCHED_AT, stamped into the pod env by launch.sh, minus a
10-min clock-skew grace), or WAIT_TIMEOUT_MIN elapses. A vendor whose manifest never turns up is
reported and skipped rather than blocking forever — a partial build with a recorded gap beats no
build. Without POST_LAUNCHED_AT (manual runs, old launcher) it falls back to the legacy check —
manifest dated today in UTC — which fails BOTH ways around midnight UTC (observed 2026-08-24/25):
a prior run ending after 00:00 UTC pre-satisfies the next evening's gate (m1 builds before the
fetch finishes, models silently score the prior close), and a post job launched after 00:00 UTC
waits on manifests that can never match until the timeout. Launch-relative freshness has neither
problem: the fetchers are fired moments before post, so any manifest newer than post's launch
proves a same-batch fetch completed.

Note this deliberately waits on the MANIFEST, not on pod state: a manifest dated today is proof the
fetcher reached its end and wrote its results, which pod-liveness cannot tell you (a pod can die
mid-run, and RunPod has been known to report RUNNING for a container that never started).

Exit code: 0 if both stages ran, 1 if either failed. A non-empty quarantine is not a failure.
"""
import json
import os
import runpy
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

VOLUME = os.environ.get("VOLUME_ROOT", "/workspace")
WAIT_FOR = [t for t in os.environ.get(
    "WAIT_FOR", "data,data_nasdaq,data_borrow,data_calendar").split(",") if t]
WAIT_TIMEOUT_MIN = int(os.environ.get("WAIT_TIMEOUT_MIN", "240"))
POLL_SEC = int(os.environ.get("WAIT_POLL_SEC", "60"))
CODE = os.environ.get("CODE_DIR", "/workspace/code")


def log(m):
    print(m, flush=True)


def _parse_ts(s):
    """ISO-8601 -> aware datetime, or None. Tolerates 'Z' and naive stamps (assumed UTC)."""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _ended_at(tree):
    try:
        with open(os.path.join(VOLUME, tree, "_run.json")) as f:
            return json.load(f).get("ended_at", "")
    except (OSError, ValueError, AttributeError):
        return ""


def _fresh(tree, floor, today):
    """True once <tree>/_run.json proves a fetch from THIS batch finished.

    floor set (launch-relative mode): ended_at must be newer than post's launch
    minus a clock-skew grace. floor None (legacy): ended_at carries today's UTC
    date — see the module docstring for why that misbehaves around midnight UTC.
    """
    ended = _ended_at(tree)
    if floor is not None:
        ts = _parse_ts(ended)
        return ts is not None and ts >= floor
    return str(ended)[:10] == today


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    launched = _parse_ts(os.environ.get("POST_LAUNCHED_AT", ""))
    floor = (launched - timedelta(minutes=10)) if launched else None
    deadline = time.monotonic() + WAIT_TIMEOUT_MIN * 60
    if floor is not None:
        log(f"     waiting for manifests newer than {floor.isoformat()} "
            f"(launch {launched.isoformat()} - 10 min grace): {WAIT_FOR}  "
            f"timeout {WAIT_TIMEOUT_MIN} min")
    else:
        log(f"     POST_LAUNCHED_AT not set — legacy gate. waiting for today's "
            f"({today}) manifests: {WAIT_FOR}  timeout {WAIT_TIMEOUT_MIN} min")
    while True:
        pending = [t for t in WAIT_FOR if not _fresh(t, floor, today)]
        if not pending:
            log("     all vendor manifests are current")
            break
        if time.monotonic() > deadline:
            log(f"WARN proceeding without: {pending} — their manifests never freshened "
                f"({'newer than ' + floor.isoformat() if floor else 'dated ' + today}). "
                f"The build below reflects whatever is on the volume.")
            break
        time.sleep(POLL_SEC)

    rc = 0
    for script, args, label in (("validate.py", ["--repair"], "validate"),
                                ("build_m1.py", [], "build_m1")):
        path = os.path.join(CODE, script)
        if not os.path.exists(path):
            log(f"FAIL {label}: {path} not found")
            rc = 1
            continue
        log(f"=== {label} ===")
        # Subprocess, not runpy: the two stages have module-level config read from env, and a
        # crash in one must not take the other down with it.
        env = dict(os.environ)
        env["DATA_DIR"] = os.path.join(VOLUME, "data_quality" if label == "validate" else "m1")
        env.pop("OUT_DIR", None)
        r = subprocess.run([sys.executable, path] + args, env=env)
        # A NEGATIVE returncode means the child died on a signal, not a clean error exit.
        # -9 (SIGKILL) is almost always the kernel OOM killer: on 2026-08-27 a 4 GB pod had
        # both stages killed mid-run, yet "exit=-9" read like any other failure and the pod
        # self-terminated normally, leaving a stale m1 manifest. Say plainly what happened.
        if r.returncode < 0:
            sig = -r.returncode
            hint = ("  <-- KILLED BY SIGNAL 9 (SIGKILL): almost certainly out of memory. "
                    "This stage needs >= 8 GB (4 vCPU); it did NOT finish and whatever it "
                    "had already written is a PARTIAL build." if sig == 9 else
                    f"  <-- killed by signal {sig}; the stage did NOT finish.")
            log(f"=== {label} exit={r.returncode} ==={hint}")
        else:
            log(f"=== {label} exit={r.returncode} ===")
        if r.returncode != 0:
            rc = 1
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
