#!/usr/bin/env python3
"""Post-fetch stage: wait for today's vendor runs, then validate -> build M1.

`launch.sh all` fires every fetcher in parallel, so the two stages that CONSUME their output —
validate.py (D-12/M1-04 + Q-004 + repair) and build_m1.py (the §3/§4 landing layer) — cannot be
part of it. Running them by hand afterwards works but is exactly the sort of manual step that gets
forgotten, and a forgotten m1 means models silently read yesterday's tables.

This job closes that loop: launch it at the same time as `all` and it BLOCKS until the vendor
manifests are dated today, then runs the two stages in order in a single pod.

    launch.sh all && launch.sh post      # post waits for all to finish, then validates + builds

WAITING. Polls `<tree>/_run.json` for each vendor in WAIT_FOR until every one is stamped with
today's UTC date, or WAIT_TIMEOUT_MIN elapses. A vendor whose manifest never turns up is reported
and skipped rather than blocking forever — a partial build with a recorded gap beats no build.

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
from datetime import datetime, timezone

VOLUME = os.environ.get("VOLUME_ROOT", "/workspace")
WAIT_FOR = [t for t in os.environ.get(
    "WAIT_FOR", "data,data_nasdaq,data_borrow,data_calendar").split(",") if t]
WAIT_TIMEOUT_MIN = int(os.environ.get("WAIT_TIMEOUT_MIN", "240"))
POLL_SEC = int(os.environ.get("WAIT_POLL_SEC", "60"))
CODE = os.environ.get("CODE_DIR", "/workspace/code")


def log(m):
    print(m, flush=True)


def _fresh(tree, today):
    """True once <tree>/_run.json carries today's UTC date."""
    try:
        with open(os.path.join(VOLUME, tree, "_run.json")) as f:
            ended = json.load(f).get("ended_at", "")
        return str(ended)[:10] == today
    except (OSError, ValueError, AttributeError):
        return False


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    deadline = time.monotonic() + WAIT_TIMEOUT_MIN * 60
    log(f"     waiting for today's ({today}) manifests: {WAIT_FOR}  "
        f"timeout {WAIT_TIMEOUT_MIN} min")
    while True:
        pending = [t for t in WAIT_FOR if not _fresh(t, today)]
        if not pending:
            log("     all vendor manifests are current")
            break
        if time.monotonic() > deadline:
            log(f"WARN proceeding without: {pending} — their manifests never reached {today}. "
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
