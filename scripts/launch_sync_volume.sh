#!/usr/bin/env bash
# Launch the one-shot data-sync pod: copies prediction-stack inputs from the
# production volume (default 8qik4zxpxq — READ-ONLY over S3) onto the
# experiment volume (default crimtr8kbf), then self-terminates.
#   EXP_VOLUME_ID / SRC_VOLUME_ID override the defaults; KEEP_POD=1 to inspect.
. "$(dirname "$0")/../data_acquisition/scripts/_common.sh"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

EXP_VOL="${EXP_VOLUME_ID:-crimtr8kbf}"
SRC_VOL="${SRC_VOLUME_ID:-$RUNPOD_VOLUME_ID}"
[ "$EXP_VOL" = "$SRC_VOL" ] && { echo "refusing: EXP and SRC volume are both $EXP_VOL" >&2; exit 2; }
: "${RUNPOD_API_KEY:?set in data_acquisition/runpod/.env}"

DC="${RUNPOD_DATACENTER:-EU-RO-1}"
CPU_IMAGE="${RUNPOD_IMAGE:-python:3.11-slim}"
FLAVORS="${RUNPOD_CPU_FLAVORS:-[\"cpu3c\",\"cpu3g\",\"cpu3m\",\"cpu5c\",\"cpu5g\",\"cpu5m\"]}"
VCPU="${RUNPOD_VCPU:-2}"

RUNNING=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || RUNNING=""
if printf '%s' "$RUNNING" | grep -q "investopediaclaude-sync"; then
  echo "SKIP: pod investopediaclaude-sync already running"; exit 0
fi

echo "Uploading sync bootstrap to s3://$EXP_VOL/code/sync/bootstrap.sh ..."
aws s3 cp --region "$RUNPOD_S3_REGION" --endpoint-url "$RUNPOD_S3_ENDPOINT" \
  "$REPO_ROOT/scripts/pod_bootstrap_sync.sh" "s3://$EXP_VOL/code/sync/bootstrap.sh"

PAYLOAD=$(cat <<JSON
{
  "name": "investopediaclaude-sync",
  "computeType": "CPU", "vcpuCount": ${VCPU}, "cpuFlavorIds": ${FLAVORS},
  "cloudType": "SECURE",
  "imageName": "${CPU_IMAGE}",
  "networkVolumeId": "${EXP_VOL}",
  "containerDiskInGb": ${RUNPOD_CONTAINER_DISK_GB:-10},
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/code/sync/bootstrap.sh"],
  "env": {
    "SRC_VOLUME_ID": "${SRC_VOL}",
    "AWS_ACCESS_KEY_ID": "${AWS_ACCESS_KEY_ID}",
    "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
    "RUNPOD_S3_ENDPOINT": "${RUNPOD_S3_ENDPOINT}",
    "RUNPOD_S3_REGION": "${RUNPOD_S3_REGION}",
    "RUNPOD_TERMINATE_KEY": "${RUNPOD_API_KEY}",
    "KEEP_POD": "${KEEP_POD:-}",
    "SYNC_SET": "${SYNC_SET:-inputs}"
  }
}
JSON
)
RESP=$(curl -sS -w $'\n%{http_code}' -X POST https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" -H 'Content-Type: application/json' \
  -d "$PAYLOAD")
CODE=$(printf '%s' "$RESP" | tail -n1); BODY=$(printf '%s' "$RESP" | sed '$d')
if [ "$CODE" != "200" ] && [ "$CODE" != "201" ]; then
  echo "pod create failed (HTTP $CODE):" >&2; echo "$BODY" >&2; exit 1
fi
POD_ID=$(printf '%s' "$BODY" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))')
printf '%s\tsync\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$POD_ID" \
  >> "$ROOT/runpod/launched-pods.log"
echo "launched sync pod: ${POD_ID}  ($SRC_VOL -> $EXP_VOL)"
echo "watch: aws s3 ls --region $RUNPOD_S3_REGION --endpoint-url $RUNPOD_S3_ENDPOINT s3://$EXP_VOL/_pod_logs/"
