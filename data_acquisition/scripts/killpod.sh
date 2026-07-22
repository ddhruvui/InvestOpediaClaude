#!/usr/bin/env bash
# Safety net: terminate any leftover "investopediaclaude-eodhd" pods that failed to
# self-terminate. Lists pods via the account API and DELETEs each match. Run this
# if launch.sh reported a pod but it never died (or just run it periodically).
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
    if p.get("name") != "investopediaclaude-eodhd":
        continue
    pid = p.get("id")
    req = u.Request("https://rest.runpod.io/v1/pods/" + pid, method="DELETE")
    req.add_header("Authorization", "Bearer " + key)
    try:
        st = u.urlopen(req, timeout=30).status
        print(f"deleted {pid} ({p.get(\"desiredStatus\")}) -> {st}")
        killed += 1
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"already gone {pid}")
        else:
            print(f"FAILED {pid}: HTTP {e.code}", file=sys.stderr)
    except Exception as e:
        print(f"FAILED {pid}: {e}", file=sys.stderr)
print(f"killed {killed} pod(s)")
'
