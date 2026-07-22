#!/usr/bin/env bash
# Show what's on the network volume: per-object listing + total object count & size.
. "$(dirname "$0")/_common.sh"

if [ -n "${RUNPOD_API_KEY:-}" ]; then
  curl -sS "https://rest.runpod.io/v1/networkvolumes/$RUNPOD_VOLUME_ID" \
    -H "Authorization: Bearer $RUNPOD_API_KEY" 2>/dev/null \
    | python3 -c "import json,sys;d=json.load(sys.stdin);print(f\"Volume {d.get('id')} ({d.get('name')}): {d.get('size')} GB allocated in {d.get('dataCenterId')}\")" 2>/dev/null || true
fi

echo "Contents of $BUCKET:"
aws s3 ls $S3FLAGS "$BUCKET/" --recursive --summarize --human-readable
