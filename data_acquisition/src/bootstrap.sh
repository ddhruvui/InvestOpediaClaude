#!/usr/bin/env bash
# Pod entrypoint (uploaded to the volume, run via: bash /workspace/code/bootstrap.sh).
# Runs the EODHD fetcher under a watchdog, then ALWAYS self-terminates — retrying until the
# API CONFIRMS the pod is gone (HTTP 204) or already gone (404), so a stuck/failed job can
# never keep billing. Stdlib python only (no curl, no pip).
set +e

# 8h watchdog: a cold full-universe EODHD pass (prices+divs+splits+fundamentals+estimates+news
# for ~500 tickers) runs several hours; this still bounds a hung fetch.
timeout 28800 python /workspace/code/fetch.py
ec=$?
echo "fetch=$ec — terminating pod $RUNPOD_POD_ID"

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
