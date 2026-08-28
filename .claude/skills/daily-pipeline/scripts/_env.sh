#!/usr/bin/env bash
# Shared: load runpod/.env and export the volume/S3 handles the other scripts use.
# Sourced, not executed. Keeps the volume id out of every script so this works on
# any clone (the id is per-account and lives only in .env).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
ENVF="$REPO/data_acquisition/runpod/.env"
[ -f "$ENVF" ] || { echo "missing $ENVF" >&2; exit 1; }
set -a; . "$ENVF"; set +a
: "${RUNPOD_VOLUME_ID:?set in runpod/.env}"
: "${RUNPOD_API_KEY:?set in runpod/.env}"
BUCKET="s3://$RUNPOD_VOLUME_ID"
