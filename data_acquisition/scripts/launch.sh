#!/usr/bin/env bash
# 1. Upload the EODHD fetcher + tickers to the network volume (code/).
# 2. Create a CPU pod (pinned to the volume's datacenter) that runs the fetcher and
#    self-terminates. Fire-and-forget: prints the pod id and exits.
. "$(dirname "$0")/_common.sh"

: "${EODHD_API_TOKEN:?set in runpod/.env}"
: "${RUNPOD_API_KEY:?account rpa_ key, set in runpod/.env}"
DC="${RUNPOD_DATACENTER:-EU-RO-1}"
IMAGE="${RUNPOD_IMAGE:-python:3.11-slim}"
STORE_LOGS="${STORE_LOGS:-false}"     # when true, fetch.py stores a run log on success (errors always log)

echo "Uploading code to $BUCKET/code/ ..."
aws s3 cp $S3FLAGS "$ROOT/src/fetch.py"        "$BUCKET/code/fetch.py"
aws s3 cp $S3FLAGS "$ROOT/src/bootstrap.sh"    "$BUCKET/code/bootstrap.sh"
aws s3 cp $S3FLAGS "$ROOT/config/tickers.json" "$BUCKET/code/tickers.json"

# CPU flavors RunPod may rent (cpuFlavorPriority defaults to "availability", so it
# rents whichever listed flavor is free). Valid: cpu3c cpu3g cpu3m cpu5c cpu5g cpu5m.
FLAVORS="${RUNPOD_CPU_FLAVORS:-[\"cpu3c\",\"cpu3g\",\"cpu3m\",\"cpu5c\",\"cpu5g\",\"cpu5m\"]}"

PAYLOAD=$(cat <<JSON
{
  "name": "investopediaclaude-eodhd",
  "computeType": "CPU",
  "cloudType": "SECURE",
  "vcpuCount": ${RUNPOD_VCPU:-2},
  "cpuFlavorIds": ${FLAVORS},
  "imageName": "${IMAGE}",
  "networkVolumeId": "${RUNPOD_VOLUME_ID}",
  "containerDiskInGb": 20,
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/code/bootstrap.sh"],
  "env": {
    "EODHD_API_TOKEN": "${EODHD_API_TOKEN}",
    "RUNPOD_TERMINATE_KEY": "${RUNPOD_API_KEY}",
    "STORE_LOGS": "${STORE_LOGS}"
  }
}
JSON
)

echo "Creating CPU pod in ${DC} ..."
RESP=$(curl -sS -w $'\n%{http_code}' -X POST https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD")
CODE=$(printf '%s' "$RESP" | tail -n1)
BODY=$(printf '%s' "$RESP" | sed '$d')

if [ "$CODE" != "200" ] && [ "$CODE" != "201" ]; then
  echo "pod create failed (HTTP $CODE):" >&2
  echo "$BODY" >&2
  echo "Hint: if it complains about CPU flavor, set RUNPOD_CPU_FLAVORS in runpod/.env" >&2
  echo "(valid flavors: cpu3c cpu3g cpu3m cpu5c cpu5g cpu5m)" >&2
  exit 1
fi

if command -v jq >/dev/null; then
  POD_ID=$(printf '%s' "$BODY" | jq -r '.id // empty')
else
  POD_ID=$(printf '%s' "$BODY" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
fi

if [ -n "${POD_ID:-}" ]; then
  printf '%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$POD_ID" >> "$ROOT/runpod/launched-pods.log"
fi
echo "Launched pod ${POD_ID:-?} — it will fetch EODHD data to ${BUCKET}/data/ and self-terminate."
echo "Check data later with: scripts/download.sh   (safety net if it doesn't die: scripts/killpod.sh)"
