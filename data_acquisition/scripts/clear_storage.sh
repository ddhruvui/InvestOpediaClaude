#!/usr/bin/env bash
# Delete objects from the network volume. `rm --recursive` issues per-object deletes,
# which RunPod's S3 supports.
#
#   clear_storage.sh            wipe EVERYTHING (data/, code/, logs/) — asks to confirm
#   clear_storage.sh --logs     wipe ONLY the logs (data/logs/), leaving data + code intact
#   clear_storage.sh -y         skip the confirmation prompt (combine: --logs -y)
. "$(dirname "$0")/_common.sh"

TARGET="$BUCKET/"                       # default: whole bucket
LABEL="ALL objects in $BUCKET"
YES=0
for arg in "$@"; do
  case "$arg" in
    --logs|--logs-only|logs) TARGET="$BUCKET/data/logs/"; LABEL="LOGS only ($BUCKET/data/logs/)" ;;
    -y|--yes)                YES=1 ;;
    *) echo "unknown arg: $arg (valid: --logs, -y)" >&2; exit 2 ;;
  esac
done

if [ "$YES" != 1 ]; then
  printf "This deletes %s. Continue? [y/N] " "$LABEL"
  read -r ans || { echo "aborted"; exit 0; }   # EOF (non-tty) -> abort, don't trip set -e
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "aborted"; exit 0; }
fi

aws s3 rm $S3FLAGS "$TARGET" --recursive
echo "Cleared. Remaining:"
aws s3 ls $S3FLAGS "$BUCKET/" --recursive || true
