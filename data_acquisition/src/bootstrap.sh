#!/usr/bin/env bash
# Pod entrypoint (uploaded to the volume, run via: bash /workspace/code/bootstrap.sh).
# Runs the vendor fetcher named by FETCH_SCRIPT (set by launch.sh; defaults to the EODHD
# fetch.py) under a watchdog, then tries every known way to delete its own pod.
# Self-termination is BEST EFFORT and has been failing since 2026-09-09 (RunPod 403s a
# DELETE sent from inside a pod). What actually guarantees the pod dies is the host-side
# reaper, data_acquisition/scripts/reap_pods.sh, which reads this log off the volume and
# deletes the pod from the laptop. Nothing here is allowed to be load-bearing.
# Stdlib python only (no curl, no pip).
set +e

# Mirror EVERYTHING this script and the fetcher print to the volume. The pod deletes itself at the
# end of the run, taking its container log with it, so anything that goes wrong BEFORE the fetcher's
# own logging starts (bad FETCH_SCRIPT, failed pip install, unwritable DATA_DIR, an import error at
# module scope) otherwise leaves no trace anywhere — the pod just vanishes having written nothing.
BOOT_LOG_DIR="/workspace/_pod_logs"
mkdir -p "$BOOT_LOG_DIR" 2>/dev/null
BOOT_LOG="$BOOT_LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-${FETCH_SCRIPT:-fetch.py}-${RUNPOD_POD_ID:-nopod}.log"
exec > >(tee -a "$BOOT_LOG") 2>&1
echo "bootstrap start $(date -u +%FT%TZ) pod=${RUNPOD_POD_ID:-?} script=${FETCH_SCRIPT:-fetch.py} \
data_dir=${DATA_DIR:-<fetcher default>} config=${CONFIG_PATH:-<fetcher default>}"
python -c 'import sys; print("python", sys.version)' 2>&1
ls -la /workspace/code/ 2>&1 | head -20

# RESTART GUARD. RunPod relaunches dockerStartCmd whenever it exits, so a pod that cannot
# delete itself (see the terminate ladder below) comes straight back and RE-RUNS the whole
# fetcher. On 2026-09-09 the calendar and finbert pods each ran 5 times and nasdaq twice,
# every ~5 min, which on a metered vendor is real money and real API quota.
# The marker is written AFTER the fetcher returns and holds its exit code, so a restart
# reports what the ORIGINAL run did instead of re-fetching — while a pod killed MID-fetch
# (host OOM, hardware) leaves no marker and still resumes, which is what we want there.
MARKER="/workspace/_pod_logs/.ran-${RUNPOD_POD_ID:-nopod}"
if [ -f "$MARKER" ]; then
  ec="$(cat "$MARKER" 2>/dev/null)"
  case "$ec" in ''|*[!0-9-]*) ec=98 ;; esac      # 98: marker unreadable, outcome unknown
  echo "RESTART DETECTED — ${FETCH_SCRIPT:-fetch.py} already ran on this pod (exit $ec); not re-running"
else
  # Optional pip deps. Every fetcher is stdlib-only EXCEPT fetch_calendar.py, which needs
  # `exchange_calendars` (D-11 wants FUTURE sessions, which no amount of stdlib can derive).
  # launch.sh sets PIP_PACKAGES only for the vendors that need it, so the common path stays offline.
  if [ -n "${PIP_PACKAGES:-}" ]; then
    echo "installing pip packages: $PIP_PACKAGES"
    timeout 600 python -m pip install --quiet --no-input --disable-pip-version-check $PIP_PACKAGES \
      || echo "!! pip install failed — the fetcher will report the missing import"
  fi

  # 8h watchdog: a cold full-universe pass (EODHD prices+divs+splits+fundamentals+estimates+news, or
  # Sharadar SEP+SF1+ACTIONS, for ~500 tickers) runs several hours; a bulk backfill can run longer.
  # This bounds a hung fetch without cutting a legitimate long backfill short.
  # VALIDATE_ARGS is the one job that takes CLI flags (validate.py --repair); every fetcher ignores
  # extra argv, so passing it unconditionally is harmless. Unquoted on purpose: it is a flag list.
  timeout 28800 python "/workspace/code/${FETCH_SCRIPT:-fetch.py}" ${VALIDATE_ARGS:-}
  ec=$?
  echo "$ec" > "$MARKER" 2>/dev/null || true
fi
echo "fetch=$ec ($([ $ec -eq 124 ] && echo 'WATCHDOG TIMEOUT' || echo 'exited')) at $(date -u +%FT%TZ) \
— terminating pod $RUNPOD_POD_ID"
sync 2>/dev/null   # flush the tee'd log to the network volume before the pod is destroyed

# TERMINATE. On 2026-09-09 every DELETE from inside a pod started coming back 403; the
# cause is NOT the pod and NOT the key. Cloudflare fronts rest.runpod.io and now answers
# "error code: 1010" (banned browser signature) to the DEFAULT Python User-Agent, and
# urllib sends `Python-urllib/3.11`. Proof, all from one machine, same key, same URL:
# urllib default UA -> 403, urllib with any other UA -> 404, curl -> 404, curl FORCED to
# `Python-urllib/3.11` -> 403. The laptop "it works from here" test used curl, which is
# why it looked like an inside-the-pod restriction. Hence UA below — do not remove it.
# The ladder then tries GraphQL podTerminate (different host, same UA fix) and runpodctl,
# and NAMES whichever worked, so a future block shows up in the log instead of a mystery.
# None of this is load-bearing: data_acquisition/scripts/reap_pods.sh deletes this pod
# from the host if every rung fails.
for attempt in $(seq 1 6); do
  timeout 90 python - <<'PY'
import json, os, shutil, ssl, subprocess, sys, urllib.error, urllib.request as u

pid = os.environ.get("RUNPOD_POD_ID", "")
key = os.environ.get("RUNPOD_TERMINATE_KEY", "")
if not pid or not key:
    print("MISSING RUNPOD_POD_ID or RUNPOD_TERMINATE_KEY", file=sys.stderr)
    sys.exit(2)
UA = "investopediaclaude-pod/1.0"                  # anything but Python-urllib/*; see above
CTXS = (None, ssl._create_unverified_context())    # verified TLS first, unverified fallback


def _req(url, **kw):
    r = u.Request(url, **kw)
    r.add_header("Authorization", "Bearer " + key)
    r.add_header("User-Agent", UA)
    return r


def rest():
    err = "no attempt"
    for ctx in CTXS:
        try:
            st = u.urlopen(_req("https://rest.runpod.io/v1/pods/" + pid, method="DELETE"),
                           timeout=30, context=ctx).status
            if st == 204:
                return True, "HTTP 204"
            err = "HTTP %s" % st
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True, "HTTP 404 (already gone)"
            err = "HTTP %s%s" % (e.code, " (Cloudflare 1010 — User-Agent blocked)"
                                 if e.code == 403 else "")
        except Exception as e:
            err = str(e)
    return False, err


def graphql():
    body = json.dumps({
        "query": "mutation($id: String!) { podTerminate(input: {podId: $id}) }",
        "variables": {"id": pid},
    }).encode()
    err = "no attempt"
    for ctx in CTXS:
        try:
            req = _req("https://api.runpod.io/graphql", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            payload = json.loads(u.urlopen(req, timeout=30, context=ctx).read().decode() or "{}")
            errs = payload.get("errors") or []
            if not errs:
                return True, "podTerminate accepted"
            code = (errs[0].get("extensions") or {}).get("code")
            if code == "POD_NOT_FOUND":
                return True, "podTerminate (already gone)"
            err = str(errs[0].get("message") or code)
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
        except Exception as e:
            err = str(e)
    return False, err


def runpodctl():
    exe = shutil.which("runpodctl")
    if not exe:
        return False, "not installed on this image"
    try:
        p = subprocess.run([exe, "remove", "pod", pid], timeout=60,
                           env=dict(os.environ, RUNPOD_API_KEY=key),
                           capture_output=True, text=True)
    except Exception as e:
        return False, str(e)
    out = ((p.stdout or "") + " " + (p.stderr or "")).strip().replace("\n", " ")[:160]
    return (p.returncode == 0), ("rc=%s %s" % (p.returncode, out))


for name, fn in (("rest", rest), ("graphql", graphql), ("runpodctl", runpodctl)):
    ok, msg = fn()
    if ok:
        print("TERMINATED via %s: %s" % (name, msg))
        sys.exit(0)
    print("terminate via %s failed: %s" % (name, msg), file=sys.stderr)
sys.exit(1)
PY
  [ $? -eq 0 ] && exit 0
  echo "terminate attempt $attempt did not confirm — retrying in 20s"
  sleep 20
done

# Every rung refused. Exiting hands the pod back to RunPod, which relaunches it — the
# restart guard above keeps that from re-running the job, and the host-side reaper
# (data_acquisition/scripts/reap_pods.sh, run automatically by scripts/daily.sh) deletes
# the pod within a poll or two.
echo "!! TERMINATION NOT CONFIRMED after retries — leaving this pod to the host-side reaper"
echo "   (data_acquisition/scripts/reap_pods.sh)"
sleep 30
