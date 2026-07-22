#!/usr/bin/env bash
# Download every file on the volume EXCEPT code/ into the repo root, mirroring keys
# (so data/GOOG.json -> ./data/GOOG.json). Each object is fetched with
# `s3api get-object` (a pure GetObject, no HeadObject — which RunPod 403s on freshly
# pod-written files).
. "$(dirname "$0")/_common.sh"

REPO_ROOT="$(cd "$ROOT/.." && pwd)"

# List all keys. The first list of freshly pod-written files can be slow or return a
# duplicate next-token; retry a few times before giving up.
list_keys() {
  for i in 1 2 3 4 5; do
    if KEYS=$(aws s3 ls $S3FLAGS "$BUCKET/" --recursive 2>/dev/null | awk '{$1=$2=$3=""; sub(/^ +/,""); print}'); then
      [ -n "$KEYS" ] && { printf '%s\n' "$KEYS"; return 0; }
    fi
    sleep 3
  done
  return 1
}

KEYS=$(list_keys) || { echo "nothing on $BUCKET (run scripts/launch.sh first)" >&2; exit 1; }

n=0
while IFS= read -r key; do
  [ -n "$key" ] || continue
  case "$key" in
    code/*) continue ;;   # skip uploaded code
    */) continue ;;       # skip S3 directory-marker keys
  esac
  dest="$REPO_ROOT/$key"
  mkdir -p "$(dirname "$dest")"
  echo "  $key"
  ok=0
  for i in 1 2 3 4 5; do
    # write to a temp file and rename only on success, so a failed/partial fetch
    # never leaves a truncated file behind.
    if aws s3api get-object $S3FLAGS --bucket "$RUNPOD_VOLUME_ID" --key "$key" "$dest.part" >/dev/null 2>&1; then
      mv "$dest.part" "$dest"; ok=1; break
    fi
    rm -f "$dest.part"; sleep 3
  done
  [ "$ok" = 1 ] || { echo "FAILED to download $key" >&2; exit 1; }
  n=$((n + 1))
done <<< "$KEYS"

echo "Downloaded $n file(s) to $REPO_ROOT/ (data/ at repo root)"
