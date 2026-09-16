#!/usr/bin/env bash
# Free space on the network volume, from the host: allocated size (RunPod API) minus bytes used
# (S3 listing). Prints three lines: allocated_gb, used_gb, free_gb. Used by the intraday runner to
# decide whether to grow before launching. Listing 15k objects takes ~10-20s.
. "$(dirname "$0")/_common.sh"
: "${RUNPOD_API_KEY:?account rpa_ key, set in runpod/.env}"
alloc=$(curl -sS "https://rest.runpod.io/v1/networkvolumes/$RUNPOD_VOLUME_ID" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "User-Agent: investopediaclaude-free/1.0" \
  | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["size"]))')
used_bytes=$(aws s3 ls $S3FLAGS "$BUCKET/" --recursive --summarize | awk '/Total Size/{print $3}')
python3 - "$alloc" "$used_bytes" <<'PY'
import sys
alloc=float(sys.argv[1]); used=float(sys.argv[2])/2**30
print(f"allocated_gb {alloc:.0f}"); print(f"used_gb {used:.2f}"); print(f"free_gb {alloc-used:.2f}")
PY
