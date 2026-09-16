#!/usr/bin/env python3
"""Launch the minute-bar fetcher on RunPod, wait for it, make sure the pod is gone, grow the volume
by 1 GB and relaunch if the pod could not grow it itself, then verify. Exit 0 only when verified.

    python3 .claude/skills/intraday-pull/scripts/run_intraday.py            # full cycle
    python3 .claude/skills/intraday-pull/scripts/run_intraday.py --status   # just report
Env: MIN_FREE_GB (5), MAX_ROUNDS (8), WAIT_MAX_SEC (30600), POLL_SEC (60), DRY_RUN=1, and the
launch overrides INTRADAY_RESERVE_CREDITS / INTRADAY_MAX_RUN_MINUTES / INTRADAY_WORKERS.

The pod grows the network volume itself (1 GB at a time, keeping min_free_gb free), so the runner's
grow is the fallback: before launch when free space is under MIN_FREE_GB, and after a `fetch=75`.
A run that stopped on its time cap is relaunched (credits remain); one that stopped on the credit
budget is not (the counter resets at 00:00 UTC; the nightly `launch.sh all` continues it).

APPEND-ONLY PROOF: before the first launch the runner snapshots the manifest's per-symbol row counts
and the S3 listing (size + LastModified) of a few symbols' closed-year files; afterwards no symbol
may have fewer rows and no closed-year file may have shrunk. A closed-year file changes at all only
when the one-time gap pass added bars to it — the report counts those separately.

Everything on the volume is read through `aws s3` with data_acquisition/runpod/.env; nothing is
typed. Pod self-termination is checked, and reap_pods.sh runs afterwards regardless."""
import json, os, re, subprocess, sys, time
from datetime import datetime, timezone

try:
    sys.stdout.reconfigure(line_buffering=True)   # progress must show through a pipe, not only at exit
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))          # repo root
DA = os.path.join(ROOT, "data_acquisition")
ENV = {}
for line in open(os.path.join(DA, "runpod", ".env")):
    if "=" in line and not line.startswith("#"):
        k, v = line.strip().split("=", 1); ENV[k] = v.strip().strip('"')
S3 = ["aws", "s3"]; S3FLAGS = ["--region", ENV["RUNPOD_S3_REGION"], "--endpoint-url", ENV["RUNPOD_S3_ENDPOINT"]]
BUCKET = f"s3://{ENV['RUNPOD_VOLUME_ID']}"
AWSENV = dict(os.environ, AWS_ACCESS_KEY_ID=ENV["AWS_ACCESS_KEY_ID"], AWS_SECRET_ACCESS_KEY=ENV["AWS_SECRET_ACCESS_KEY"], AWS_EC2_METADATA_DISABLED="true")
MIN_FREE = float(os.environ.get("MIN_FREE_GB", "5")); MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "8"))
WAIT_MAX = int(os.environ.get("WAIT_MAX_SEC", "30600")); POLL = int(os.environ.get("POLL_SEC", "60"))
DRY = os.environ.get("DRY_RUN", "")
STORE = "data/tickdata"
PROOF_SYMBOLS = ("MU", "AAPL", "SPY")


def sh(cmd, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, env=env or AWSENV)


def s3ls(prefix):
    p = sh(S3 + ["ls"] + S3FLAGS + [f"{BUCKET}/{prefix}"])
    return [l.split()[-1] for l in p.stdout.splitlines() if l.strip()]


def s3ls_detail(prefix):
    p = sh(S3 + ["ls"] + S3FLAGS + [f"{BUCKET}/{prefix}"])
    out = {}
    for l in p.stdout.splitlines():
        parts = l.split()
        if len(parts) == 4:
            out[parts[3]] = (int(parts[2]), f"{parts[0]} {parts[1]}")
    return out


def s3cat(key):
    p = sh(S3 + ["cp"] + S3FLAGS + [f"{BUCKET}/{key}", "-"])
    return p.stdout if p.returncode == 0 else ""


def free_gb():
    """Free space on the volume. The pod does the real accounting (and grows the volume itself), so
    this pre-launch check only has to be roughly right: use the last run's `free_gb_at_end` when it is
    under a day old, else fall back to volume_free.sh's full S3 listing — which is slow (the listing
    of ~22k objects sat for 5+ minutes on 2026-09-15) and therefore bounded to FREE_CHECK_SEC."""
    try:
        run = json.loads(s3cat(f"{STORE}/_run.json") or "{}")
        ended = datetime.fromisoformat(run["ended_at"]).timestamp()
        if run.get("free_gb_at_end") is not None and time.time() - ended < 86400:
            f = float(run["free_gb_at_end"])
            print(f"free_gb {f:.2f} (pod's accounting at {run['ended_at'][:19]}; the pod re-measures and grows on its own)")
            return f
    except Exception:
        pass
    try:
        p = subprocess.run(["bash", os.path.join(DA, "scripts", "volume_free.sh")], capture_output=True, text=True,
                           env=dict(os.environ), timeout=int(os.environ.get("FREE_CHECK_SEC", "300")))
        m = re.search(r"free_gb\s+(-?[\d.]+)", p.stdout)
        print(p.stdout.strip())
        return float(m.group(1)) if m else float("inf")
    except subprocess.TimeoutExpired:
        print("free-space listing timed out — launching anyway; the pod measures free space itself and grows the volume")
        return float("inf")


def grow(step=1):
    p = sh(["bash", os.path.join(DA, "scripts", "grow_volume.sh"), str(step)], env=dict(os.environ))
    print(p.stdout.strip(), p.stderr.strip()); return p.returncode == 0


def pods():
    p = sh(["curl", "-sS", "https://rest.runpod.io/v1/pods", "-H", f"Authorization: Bearer {ENV['RUNPOD_API_KEY']}",
            "-H", "User-Agent: investopediaclaude-runner/1.0"])
    try:
        d = json.loads(p.stdout)
        return d if isinstance(d, list) else d.get("pods", d.get("data", []))
    except Exception:
        return None


def running_intraday_pod():
    for pod in pods() or []:
        if str(pod.get("name", "")).startswith("investopediaclaude-intraday"):
            return pod.get("id")
    return None


def launch():
    env = dict(os.environ, STORE_LOGS="true")
    if DRY: env["DRY_RUN"] = "1"
    p = subprocess.run(["bash", os.path.join(DA, "scripts", "launch.sh"), "intraday"], capture_output=True, text=True, env=env)
    print(p.stdout.strip()); print(p.stderr.strip(), file=sys.stderr)
    if DRY: return None
    if "SKIP intraday" in p.stdout:
        pid = running_intraday_pod(); print(f"intraday pod already running: {pid}"); return pid
    m = re.findall(r"Launched intraday pod (\S+)", p.stdout)
    return m[-1] if m else running_intraday_pod()


def wait_for_exit(pod):
    """Poll _pod_logs for this pod's log; return (rc, log text) once `fetch=<rc>` shows."""
    t = 0; last_line = ""
    while t < WAIT_MAX:
        logs = sorted(k for k in s3ls("_pod_logs/") if k.endswith(f"-fetch_intraday.py-{pod}.log"))
        if logs:
            txt = s3cat(f"_pod_logs/{logs[0]}")        # oldest log of this pod = the real run (restart guard)
            m = re.findall(r"^fetch=(-?\d+)", txt, re.M)
            if m:
                return int(m[-1]), txt
            lines = [l for l in txt.splitlines() if l.startswith("[") or l.startswith("     ..") or "space:" in l or "NEED_SPACE" in l]
            if lines and lines[-1] != last_line:
                last_line = lines[-1]
                print(f"  {datetime.now(timezone.utc).strftime('%H:%M')}Z {last_line.strip()[:140]}")
        time.sleep(POLL); t += POLL
    return None, ""


def pod_gone(pod):
    for _ in range(6):
        ps = pods()
        if ps is not None and not any(p.get("id") == pod for p in ps):
            return True
        time.sleep(20)
    return False


def reap():
    p = sh(["bash", os.path.join(DA, "scripts", "reap_pods.sh"), "intraday"], env=dict(os.environ))
    print(p.stdout.strip()[-600:])


def verify():
    return subprocess.run([sys.executable, os.path.join(HERE, "verify_intraday.py")], env=AWSENV).returncode


def snapshot():
    man = json.loads(s3cat(f"{STORE}/_manifest.json") or "{}").get("symbols", {})
    files = {s: s3ls_detail(f"{STORE}/1m/{s}/") for s in PROOF_SYMBOLS}
    return {"rows": {s: int(v.get("rows") or 0) for s, v in man.items()}, "files": files}


def append_only_report(before):
    after = snapshot()
    cur_year = str(datetime.now(timezone.utc).year)
    shrunk_rows = [(s, n, after["rows"].get(s, 0)) for s, n in before["rows"].items() if after["rows"].get(s, 0) < n]
    same = grew = 0; bad = []
    for s, files in before["files"].items():
        for name, (size, mod) in files.items():
            if name.startswith(cur_year):
                continue
            a = after["files"].get(s, {}).get(name)
            if a is None:
                bad.append(f"{s}/{name} vanished")
            elif a == (size, mod):
                same += 1
            elif a[0] > size:
                grew += 1
            else:
                bad.append(f"{s}/{name} size {size} -> {a[0]}")
    grown_syms = sum(1 for s, n in before["rows"].items() if after["rows"].get(s, 0) > n)
    print(f"append-only check: {len(before['rows'])} symbols held before, {grown_syms} gained rows, {len(shrunk_rows)} lost rows; "
          f"closed-year files of {'/'.join(PROOF_SYMBOLS)}: {same} byte-for-byte untouched, {grew} gained bars (gap pass), {len(bad)} shrank/vanished")
    for x in shrunk_rows[:10]: print(f"  !! {x[0]} rows {x[1]:,} -> {x[2]:,}")
    for x in bad[:10]: print(f"  !! {x}")
    return not shrunk_rows and not bad


def status():
    pod = running_intraday_pod(); print("running intraday pod:", pod or "none")
    return verify()


def main():
    if "--status" in sys.argv:
        return status()
    before = None if DRY else snapshot()
    for rnd in range(1, MAX_ROUNDS + 1):
        print(f"=== round {rnd} ===")
        f = free_gb()
        while f < MIN_FREE:
            print(f"free {f:.2f} GB < {MIN_FREE} GB — growing by 1 GB before launch")
            if not grow(1): return 2
            f = free_gb()
        pod = launch()
        if DRY: return 0
        if not pod:
            print("no pod id — launch failed"); return 2
        rc, txt = wait_for_exit(pod)
        term = re.findall(r"TERMINATED via \S+: .*|TERMINATION NOT CONFIRMED.*", txt)
        print(f"pod {pod}: fetch={rc}; self-termination: {term[-1] if term else 'no line yet'}")
        if rc is None:
            print("no exit code within WAIT_MAX — pod left to reap_pods.sh --watch"); return 3
        gone = pod_gone(pod)
        if not gone:
            reap(); gone = pod_gone(pod)
        print(f"pod {pod} {'is gone' if gone else 'STILL LISTED after reaping'}")
        for l in txt.splitlines():
            if l.startswith("verify") or l.startswith("     space:") or l.startswith("     credits:"):
                print("  " + l.strip()[:220])
        run = json.loads(s3cat(f"{STORE}/_run.json") or "{}")
        if rc == 75:
            print("NEED_SPACE and the pod could not grow the volume — growing by 1 GB from here and relaunching")
            if not grow(1): return 2
            continue
        if rc == 0 and run.get("stop_reason") == "time":
            print("stopped on its time cap with credits left — relaunching to continue")
            continue
        if run.get("stop_reason") == "budget":
            print("stopped on the credit budget — continues after 00:00 UTC (nightly launch.sh all, or rerun this)")
        break
    ok_append = append_only_report(before)
    rc_v = verify()
    return rc_v if ok_append else 1


if __name__ == "__main__":
    sys.exit(main())
