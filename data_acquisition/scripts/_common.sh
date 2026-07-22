#!/usr/bin/env bash
# Sourced by launch.sh / download.sh / clear_storage.sh / storage_usage.sh / killpod.sh.
# Loads runpod/.env and sets the S3 flags + bucket used by every script.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"             # data_acquisition/
ENV_FILE="$ROOT/runpod/.env"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE — copy runpod/.env.example and fill it in" >&2; exit 1; }
set -a; . "$ENV_FILE"; set +a

: "${AWS_ACCESS_KEY_ID:?set in runpod/.env}"
: "${AWS_SECRET_ACCESS_KEY:?set in runpod/.env}"
: "${RUNPOD_VOLUME_ID:?set in runpod/.env}"
: "${RUNPOD_S3_REGION:?set in runpod/.env}"
: "${RUNPOD_S3_ENDPOINT:?set in runpod/.env}"

S3FLAGS="--region $RUNPOD_S3_REGION --endpoint-url $RUNPOD_S3_ENDPOINT"
BUCKET="s3://$RUNPOD_VOLUME_ID"

command -v aws >/dev/null || { echo "aws CLI not found — install awscli" >&2; exit 1; }
