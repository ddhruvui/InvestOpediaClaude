#!/usr/bin/env bash
# One launcher, multiple vendors. Pick which fetcher(s) the pod(s) run:
#   scripts/launch.sh            # EODHD  (default) -> src/fetch.py        + config/tickers.json
#   scripts/launch.sh eodhd      # same as above
#   scripts/launch.sh nasdaq     # Sharadar/Nasdaq Data Link -> src/fetch_nasdaq.py + config/sharadar.json
#   scripts/launch.sh tiingo     # Tiingo (tertiary cross-check) -> src/fetch_tiingo.py + config/tiingo.json
#   scripts/launch.sh all        # the DAILY ROUTINE: eodhd + nasdaq + tiingo, one pod each. Tiingo's
#                                # skip_fresh_days makes its warm runs near-free (fresh files skipped,
#                                # budget overruns defer), so daily inclusion costs ~nothing.
#
# For each requested vendor it (1) uploads that fetcher + its config to the network volume (code/),
# then (2) creates a CPU pod (pinned to the volume's datacenter) that runs the fetcher and
# self-terminates. The vendors share the volume without colliding (distinct data namespaces).
# Fire-and-forget: prints the pod id(s) and exits. download/clear/storage_usage/killpod are shared.
#
# DRY_RUN=1 prints what would be uploaded/launched without touching S3 or creating pods.
. "$(dirname "$0")/_common.sh"

case "${1:-eodhd}" in
  all)            VENDORS="eodhd nasdaq tiingo" ;;
  eodhd)          VENDORS="eodhd" ;;
  nasdaq|sharadar) VENDORS="nasdaq" ;;
  tiingo)         VENDORS="tiingo" ;;
  *)
    echo "unknown vendor '$1' (valid: eodhd, nasdaq, tiingo, all)" >&2; exit 2 ;;
esac
: "${RUNPOD_API_KEY:?account rpa_ key, set in runpod/.env}"

DC="${RUNPOD_DATACENTER:-EU-RO-1}"
IMAGE="${RUNPOD_IMAGE:-python:3.11-slim}"
STORE_LOGS="${STORE_LOGS:-false}"     # when true, the fetcher stores a run log on success (errors always log)
DRY_RUN="${DRY_RUN:-}"

# CPU flavors RunPod may rent (cpuFlavorPriority defaults to "availability", so it
# rents whichever listed flavor is free). Valid: cpu3c cpu3g cpu3m cpu5c cpu5g cpu5m.
FLAVORS="${RUNPOD_CPU_FLAVORS:-[\"cpu3c\",\"cpu3g\",\"cpu3m\",\"cpu5c\",\"cpu5g\",\"cpu5m\"]}"

FAILED=""

# One pod per vendor at a time: a vendor whose pod is still running (e.g. tiingo's ~5h paced run)
# is skipped, not doubled — makes a daily `all` idempotent. Fail-open: if the list call errors,
# RUNNING_PODS is empty and launches proceed.
RUNNING_PODS=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || RUNNING_PODS=""

launch_vendor() {
  local VENDOR="$1" FETCH_SCRIPT CONFIG_FILE TOKEN_VAR TOKEN_VAL
  case "$VENDOR" in
    eodhd)
      FETCH_SCRIPT="fetch.py";        CONFIG_FILE="tickers.json"
      TOKEN_VAR="EODHD_API_TOKEN";    TOKEN_VAL="${EODHD_API_TOKEN:-}" ;;
    nasdaq)
      FETCH_SCRIPT="fetch_nasdaq.py"; CONFIG_FILE="sharadar.json"
      TOKEN_VAR="SHARADAR_API_KEY";   TOKEN_VAL="${SHARADAR_API_KEY:-${NASDAQ_DATA_LINK_API_KEY:-}}" ;;
    tiingo)
      FETCH_SCRIPT="fetch_tiingo.py"; CONFIG_FILE="tiingo.json"
      TOKEN_VAR="TIINGO_API_TOKEN";   TOKEN_VAL="${TIINGO_API_TOKEN:-}" ;;
  esac
  if [ -z "$TOKEN_VAL" ]; then
    echo "SKIP $VENDOR: $TOKEN_VAR not set in runpod/.env" >&2
    return 1
  fi
  if printf '%s' "$RUNNING_PODS" | grep -q "investopediaclaude-${VENDOR}"; then
    echo "SKIP $VENDOR: pod investopediaclaude-${VENDOR} is already running (its run is in progress)"
    return 0
  fi

  if [ -n "$DRY_RUN" ]; then
    echo "DRY_RUN: would upload src/$FETCH_SCRIPT + src/bootstrap.sh + config/$CONFIG_FILE to $BUCKET/code/"
    echo "DRY_RUN: would create CPU pod investopediaclaude-${VENDOR} in ${DC} (FETCH_SCRIPT=$FETCH_SCRIPT, token=$TOKEN_VAR)"
    return 0
  fi

  echo "Uploading $VENDOR code to $BUCKET/code/ ..."
  aws s3 cp $S3FLAGS "$ROOT/src/$FETCH_SCRIPT"      "$BUCKET/code/$FETCH_SCRIPT"
  aws s3 cp $S3FLAGS "$ROOT/src/bootstrap.sh"       "$BUCKET/code/bootstrap.sh"
  aws s3 cp $S3FLAGS "$ROOT/config/$CONFIG_FILE"    "$BUCKET/code/$CONFIG_FILE"

  local PAYLOAD RESP CODE BODY POD_ID
  PAYLOAD=$(cat <<JSON
{
  "name": "investopediaclaude-${VENDOR}",
  "computeType": "CPU",
  "cloudType": "SECURE",
  "vcpuCount": ${RUNPOD_VCPU:-2},
  "cpuFlavorIds": ${FLAVORS},
  "imageName": "${IMAGE}",
  "networkVolumeId": "${RUNPOD_VOLUME_ID}",
  "containerDiskInGb": ${RUNPOD_CONTAINER_DISK_GB:-20},
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/code/bootstrap.sh"],
  "env": {
    "${TOKEN_VAR}": "${TOKEN_VAL}",
    "TIINGO_API_TOKEN2": "${TIINGO_API_TOKEN2:-}",
    "FETCH_SCRIPT": "${FETCH_SCRIPT}",
    "CONFIG_PATH": "/workspace/code/${CONFIG_FILE}",
    "RUNPOD_TERMINATE_KEY": "${RUNPOD_API_KEY}",
    "STORE_LOGS": "${STORE_LOGS}"
  }
}
JSON
)

  echo "Creating $VENDOR CPU pod in ${DC} ..."
  RESP=$(curl -sS -w $'\n%{http_code}' -X POST https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer ${RUNPOD_API_KEY}" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD")
  CODE=$(printf '%s' "$RESP" | tail -n1)
  BODY=$(printf '%s' "$RESP" | sed '$d')

  if [ "$CODE" != "200" ] && [ "$CODE" != "201" ]; then
    echo "$VENDOR pod create failed (HTTP $CODE):" >&2
    echo "$BODY" >&2
    echo "Hint: if it complains about CPU flavor, set RUNPOD_CPU_FLAVORS in runpod/.env" >&2
    echo "(valid flavors: cpu3c cpu3g cpu3m cpu5c cpu5g cpu5m)" >&2
    return 1
  fi

  if command -v jq >/dev/null; then
    POD_ID=$(printf '%s' "$BODY" | jq -r '.id // empty')
  else
    POD_ID=$(printf '%s' "$BODY" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
  fi

  if [ -n "${POD_ID:-}" ]; then
    printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$VENDOR" "$POD_ID" >> "$ROOT/runpod/launched-pods.log"
  fi
  echo "Launched $VENDOR pod ${POD_ID:-?} — it will fetch data to ${BUCKET}/ and self-terminate."
}

# _common.sh sets -e; each leg runs under `if` so one vendor's failure still launches the rest,
# and the script exits nonzero listing what failed.
for V in $VENDORS; do
  if ! launch_vendor "$V"; then
    FAILED="$FAILED $V"
  fi
done

if [ -n "$FAILED" ]; then
  echo "FAILED to launch:$FAILED" >&2
  exit 1
fi
[ -n "$DRY_RUN" ] && exit 0
echo "Check data later with: scripts/download.sh   (safety net if it doesn't die: scripts/killpod.sh)"
