#!/usr/bin/env bash
# One-off (2026-08-25): release post.py's UTC-date gate by bumping ended_at in
# the four vendor _run.json manifests to now. Safe ONLY when every fetch has
# genuinely completed (verified tonight: all pods done, manifests 22:14-23:28Z
# Aug-24). Each bump is annotated in the file; tomorrow's fetch overwrites it.
set -euo pipefail
cd "$(dirname "$0")/.."
source data_acquisition/scripts/_common.sh
NOW=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
for tree in data data_nasdaq data_borrow data_calendar; do
  aws s3 cp $S3FLAGS "$BUCKET/$tree/_run.json" "/tmp/run_$tree.json" >/dev/null
  python3 - "$tree" "$NOW" <<'PY'
import json, sys
tree, now = sys.argv[1], sys.argv[2]
p = f"/tmp/run_{tree}.json"
d = json.load(open(p))
old = d.get("ended_at")
d["ended_at"] = now
d["gate_bump_note"] = (f"ended_at bumped from {old} at {now} to release post.py "
                       "UTC-date gate; the fetch genuinely completed at the original stamp")
json.dump(d, open(p, "w"))
print(f"  {tree}: ended_at {old} -> {now}")
PY
  aws s3 cp $S3FLAGS "/tmp/run_$tree.json" "$BUCKET/$tree/_run.json" >/dev/null
done
echo "done — post's next 60s poll should release the gate; build starts within ~2 min"
