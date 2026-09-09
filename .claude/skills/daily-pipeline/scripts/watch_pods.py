#!/usr/bin/env python3
"""Pod watchdog. Prints one line per STATE CHANGE, so it is safe to leave attached
to a Monitor without flooding it:

    UP <name>       pod appeared
    DONE <name>     pod gone (self-terminated, or reaped by reap_pods.sh)
    STALL <name>    pod alive but its _pod_logs entry has not grown for STALL_CHECKS polls
    IDLE            no pods running (printed once per idle stretch)

post is exempt from STALL: post.py prints its gate line once and then polls in
silence, so a flat log is its normal waiting state, not a hang.

Written in Python because macOS ships bash 3.2, which has no associative arrays —
a bash version of this silently mis-tracked state.

Env: POLL (default 180s), STALL_CHECKS (default 5, so ~15 min).
"""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
POLL = int(os.environ.get("POLL", "180"))
STALL_CHECKS = int(os.environ.get("STALL_CHECKS", "5"))
EXEMPT = {"investopediaclaude-post"}

REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
env = {}
for ln in open(os.path.join(REPO, "data_acquisition/runpod/.env")):
    ln = ln.strip()
    if ln and not ln.startswith("#") and "=" in ln:
        k, v = ln.split("=", 1)
        env[k] = v.strip().strip('"').strip("'")
KEY = env["RUNPOD_API_KEY"]


def out(m):
    print(m, flush=True)


def pods():
    r = subprocess.run(["curl", "-sS", "--max-time", "30", "https://rest.runpod.io/v1/pods",
                        "-H", f"Authorization: Bearer {KEY}"], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        d = json.loads(r.stdout)
    except Exception:
        return None
    lst = d if isinstance(d, list) else d.get("pods", d.get("data", []))
    return {p.get("name"): p.get("id") for p in lst if p.get("name")}


def logsizes():
    r = subprocess.run([os.path.join(HERE, "vol"), "ls", "_pod_logs/"],
                       capture_output=True, text=True)
    m = {}
    for ln in r.stdout.splitlines():
        f = ln.split()
        if len(f) >= 4:
            m[f[3]] = f[2]
    return m


size, stale, seen, prev = {}, {}, set(), set()
idle_said = False
while True:
    cur = pods()
    if cur is None:
        time.sleep(POLL); continue
    ls = logsizes()
    for name, pid in cur.items():
        if name not in seen:
            out(f"UP    {name} ({pid})"); seen.add(name)
        sz = next((v for k, v in ls.items() if k.endswith(f"-{pid}.log")), "0")
        if name in EXEMPT:
            size[name] = sz; continue
        if sz == size.get(name):
            stale[name] = stale.get(name, 0) + 1
            if stale[name] == STALL_CHECKS:
                out(f"STALL {name} ({pid}) log flat at {sz}B for "
                    f"{STALL_CHECKS * POLL // 60}min — investigate")
        else:
            stale[name] = 0
        size[name] = sz
    for name in prev - set(cur):
        out(f"DONE  {name} — pod gone")
        seen.discard(name)
    prev = set(cur)
    if not cur:
        if not idle_said:
            out("IDLE  no pods running"); idle_said = True
    else:
        idle_said = False
    time.sleep(POLL)
