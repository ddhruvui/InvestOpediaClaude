#!/usr/bin/env bash
# BLUNT safety net: terminate EVERY "investopediaclaude-*" pod, whether or not its job has
# finished. That makes it unsafe mid-run — it will kill a fetcher three hours into a cold
# pull. Prefer scripts/reap_pods.sh, which deletes a pod only after reading its log and
# seeing the job finish. Use this one only to clear the decks when nothing should be up.
#
# The DELETE below sends an explicit User-Agent: Cloudflare fronts rest.runpod.io and
# answers 403 "error code: 1010" to the default `Python-urllib/*` signature, which silently
# broke this script (and every pod's self-termination) on 2026-09-09.
. "$(dirname "$0")/_common.sh"
: "${RUNPOD_API_KEY:?account rpa_ key, set in runpod/.env}"

curl -sS https://rest.runpod.io/v1/pods -H "Authorization: Bearer $RUNPOD_API_KEY" \
  | RUNPOD_API_KEY="$RUNPOD_API_KEY" python3 -c '
import json, os, sys, urllib.error, urllib.request as u
key = os.environ["RUNPOD_API_KEY"]
pods = json.load(sys.stdin)
pods = pods if isinstance(pods, list) else pods.get("pods", pods.get("data", []))
killed = 0
for p in pods:
    if not str(p.get("name") or "").startswith("investopediaclaude-"):
        continue
    pid = p.get("id")
    req = u.Request("https://rest.runpod.io/v1/pods/" + pid, method="DELETE")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("User-Agent", "investopediaclaude-killpod/1.0")
    try:
        st = u.urlopen(req, timeout=30).status
        print(f"deleted {pid} ({p.get(\"desiredStatus\")}) -> {st}")
        killed += 1
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"already gone {pid}")
        elif e.code == 403:
            print(f"FAILED {pid}: HTTP 403 — Cloudflare blocked the User-Agent", file=sys.stderr)
        else:
            print(f"FAILED {pid}: HTTP {e.code}", file=sys.stderr)
    except Exception as e:
        print(f"FAILED {pid}: {e}", file=sys.stderr)
print(f"killed {killed} pod(s)")
'
