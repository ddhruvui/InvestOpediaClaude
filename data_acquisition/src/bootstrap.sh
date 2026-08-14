#!/usr/bin/env bash
# Pod entrypoint (uploaded to the volume, run via: bash /workspace/code/bootstrap.sh).
# Runs the vendor fetcher named by FETCH_SCRIPT (set by launch.sh; defaults to the EODHD
# fetch.py) under a watchdog, then ALWAYS self-terminates — retrying until the API CONFIRMS the
# pod is gone (HTTP 204) or already gone (404), so a stuck/failed job can never keep billing.
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
echo "fetch=$ec ($([ $ec -eq 124 ] && echo 'WATCHDOG TIMEOUT' || echo 'exited')) at $(date -u +%FT%TZ) \
— terminating pod $RUNPOD_POD_ID"
sync 2>/dev/null   # flush the tee'd log to the network volume before the pod is destroyed

# DELETE the pod via the REST API using the ACCOUNT key (RUNPOD_TERMINATE_KEY); the
# pod-injected RUNPOD_API_KEY is pod-scoped and 403s on delete. Success is asserted
# only on HTTP 204/404 (not just "no exception"). Each attempt is timeout-bounded so a
# DNS/network stall can't hang the terminator; verified TLS first, unverified fallback.
for attempt in $(seq 1 12); do
  timeout 60 python - <<'PY'
import os, ssl, sys, urllib.error, urllib.request as u
pid = os.environ.get("RUNPOD_POD_ID", "")
key = os.environ.get("RUNPOD_TERMINATE_KEY", "")
if not pid or not key:
    print("MISSING RUNPOD_POD_ID or RUNPOD_TERMINATE_KEY", file=sys.stderr)
    sys.exit(2)
url = "https://rest.runpod.io/v1/pods/" + pid

def kill(ctx):
    req = u.Request(url, method="DELETE")
    req.add_header("Authorization", "Bearer " + key)
    return u.urlopen(req, timeout=30, context=ctx).status

for ctx in (None, ssl._create_unverified_context()):
    try:
        st = kill(ctx)
        if st == 204:
            print("terminated (204)")
            sys.exit(0)
        print("unexpected terminate status:", st, file=sys.stderr)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("already gone (404)")
            sys.exit(0)
        print("terminate HTTPError:", e.code, file=sys.stderr)
    except Exception as e:
        print("terminate error:", e, file=sys.stderr)
sys.exit(1)
PY
  [ $? -eq 0 ] && exit 0
  echo "terminate attempt $attempt did not confirm — retrying in 20s"
  sleep 20
done

echo "!! TERMINATION NOT CONFIRMED after retries — run scripts/killpod.sh to kill this pod"
sleep 30
