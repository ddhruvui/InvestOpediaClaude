#!/usr/bin/env bash
# Sourced by every launcher/watcher in scripts/ (a copy of the DataAcquistion repo's _common.sh —
# the two repos share the volume and the RunPod account, not code).
# Loads runpod/.env and sets the S3 flags + bucket used by every script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"             # repo root
ENV_FILE="$ROOT/runpod/.env"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE — copy runpod/.env.example and fill it in" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

# Experiment volumes (branch runs): RUNPOD_VOLUME_ID_OVERRIDE redirects every
# script sourcing this file to another volume WITHOUT touching runpod/.env —
# sourcing .env above would clobber a plain env override.
if [ -n "${RUNPOD_VOLUME_ID_OVERRIDE:-}" ]; then
  RUNPOD_VOLUME_ID="$RUNPOD_VOLUME_ID_OVERRIDE"
fi

: "${AWS_ACCESS_KEY_ID:?set in runpod/.env}"
: "${AWS_SECRET_ACCESS_KEY:?set in runpod/.env}"
: "${RUNPOD_VOLUME_ID:?set in runpod/.env}"
: "${RUNPOD_S3_REGION:?set in runpod/.env}"
: "${RUNPOD_S3_ENDPOINT:?set in runpod/.env}"

S3FLAGS="--region $RUNPOD_S3_REGION --endpoint-url $RUNPOD_S3_ENDPOINT"
BUCKET="s3://$RUNPOD_VOLUME_ID"

command -v aws >/dev/null || { echo "aws CLI not found — install awscli" >&2; exit 1; }
