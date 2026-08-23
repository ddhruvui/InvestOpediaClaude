#!/usr/bin/env bash
# Launch the prediction stack on RunPod against the data volume.
#   scripts/launch_predict.sh test      # run the T-01..T-15 suite on a CPU pod
#   scripts/launch_predict.sh market    # build whole-market panel + universe (m1x) — CPU
#   scripts/launch_predict.sh stage1    # Stage-1 pipeline (features->LGBM->backtest->gates) — CPU
#   scripts/launch_predict.sh stage2    # Stage-2 GRU+CNN+FinBERT — GPU
#   scripts/launch_predict.sh predict   # latest-date scores -> target book -> suggestions — CPU
#
# USE_MARKET=1 (default) points stage1/predict at the m1x whole-market universe;
# KEEP_POD=1 leaves the pod alive for inspection. Reuses data_acquisition's .env.
. "$(dirname "$0")/../data_acquisition/scripts/_common.sh"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

JOB="${1:-stage1}"
case "$JOB" in test|market|stage1|stage2|stage3|predict) ;; *)
  echo "unknown job '$JOB' (test|market|stage1|stage2|stage3|predict)" >&2; exit 2 ;; esac
: "${RUNPOD_API_KEY:?set in data_acquisition/runpod/.env}"

DC="${RUNPOD_DATACENTER:-EU-RO-1}"
CPU_IMAGE="${RUNPOD_IMAGE:-python:3.11-slim}"
GPU_IMAGE="${RUNPOD_GPU_IMAGE:-runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04}"
FLAVORS="${RUNPOD_CPU_FLAVORS:-[\"cpu3c\",\"cpu3g\",\"cpu3m\",\"cpu5c\",\"cpu5g\",\"cpu5m\"]}"
GPU_TYPES="${RUNPOD_GPU_TYPES:-[\"NVIDIA GeForce RTX 4090\",\"NVIDIA RTX A5000\",\"NVIDIA A40\"]}"
VCPU="${RUNPOD_VCPU:-8}"        # stage1/market hold multi-GB panels: 8 vCPU -> 16 GB
PIP_CPU="pandas pyarrow numpy PyYAML scipy lightgbm scikit-learn optuna pytest"
PIP_GPU="pandas pyarrow numpy PyYAML scipy lightgbm scikit-learn optuna pytest transformers==4.44.2 sentencepiece"

RUNNING=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || RUNNING=""
if printf '%s' "$RUNNING" | grep -q "investopediaclaude-predict-${JOB}"; then
  echo "SKIP: pod investopediaclaude-predict-${JOB} already running"; exit 0
fi

echo "Bundling prediction stack ..."
TMP_TGZ="$(mktemp -t predict-bundle).tgz"
( cd "$REPO_ROOT" && tar czf "$TMP_TGZ" \
    --exclude='__pycache__' --exclude='.pytest_cache' \
    src configs tests requirements.txt )
if [ -n "${DRY_RUN:-}" ]; then
  echo "DRY_RUN: would upload $(du -h "$TMP_TGZ" | cut -f1) bundle + bootstrap; launch $JOB pod"
  exit 0
fi
aws s3 cp $S3FLAGS "$TMP_TGZ" "$BUCKET/code/predict/bundle.tgz"
aws s3 cp $S3FLAGS "$REPO_ROOT/scripts/pod_bootstrap_predict.sh" "$BUCKET/code/predict/bootstrap.sh"
rm -f "$TMP_TGZ"

ENV_COMMON=$(cat <<JSON
    "JOB": "${JOB}",
    "USE_MARKET": "${USE_MARKET:-1}",
    "KEEP_POD": "${KEEP_POD:-}",
    "RUNPOD_TERMINATE_KEY": "${RUNPOD_API_KEY}",
    "OUT_DIR": "/workspace/derived/${JOB}",
    "SCORES_DIR": "${SCORES_DIR:-/workspace/derived/stage2}"
JSON
)

if [ "$JOB" = "stage2" ]; then
  PAYLOAD=$(cat <<JSON
{
  "name": "investopediaclaude-predict-${JOB}",
  "computeType": "GPU",
  "cloudType": "SECURE",
  "gpuCount": 1,
  "gpuTypeIds": ${GPU_TYPES},
  "imageName": "${GPU_IMAGE}",
  "networkVolumeId": "${RUNPOD_VOLUME_ID}",
  "containerDiskInGb": ${RUNPOD_CONTAINER_DISK_GB:-40},
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/code/predict/bootstrap.sh"],
  "env": { "PIP_PACKAGES": "${PIP_GPU}", ${ENV_COMMON} }
}
JSON
)
else
  PAYLOAD=$(cat <<JSON
{
  "name": "investopediaclaude-predict-${JOB}",
  "computeType": "CPU",
  "cloudType": "SECURE",
  "vcpuCount": ${VCPU},
  "cpuFlavorIds": ${FLAVORS},
  "imageName": "${CPU_IMAGE}",
  "networkVolumeId": "${RUNPOD_VOLUME_ID}",
  "containerDiskInGb": ${RUNPOD_CONTAINER_DISK_GB:-20},
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/code/predict/bootstrap.sh"],
  "env": { "PIP_PACKAGES": "${PIP_CPU}", ${ENV_COMMON} }
}
JSON
)
fi

echo "Creating ${JOB} pod in ${DC} ..."
RESP=$(curl -sS -w $'\n%{http_code}' -X POST https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" -H 'Content-Type: application/json' \
  -d "$PAYLOAD")
CODE=$(printf '%s' "$RESP" | tail -n1); BODY=$(printf '%s' "$RESP" | sed '$d')
if [ "$CODE" != "200" ] && [ "$CODE" != "201" ]; then
  echo "pod create failed (HTTP $CODE):" >&2; echo "$BODY" >&2; exit 1
fi
POD_ID=$(printf '%s' "$BODY" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)
printf '%s\tpredict-%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$JOB" "$POD_ID" \
  >> "$ROOT/runpod/launched-pods.log"
echo "launched predict-${JOB} pod: ${POD_ID}"
echo "watch:   data_acquisition/scripts/storage_usage.sh | grep -E '_pod_logs|derived'"
echo "fetch:   aws s3 cp \$S3FLAGS $BUCKET/derived/${JOB}/ ./derived_${JOB}/ --recursive"
