#!/usr/bin/env bash
# Publish a console bundle for a NAMED research era from this machine, keeping nothing local.
#
# The nightly path needs none of this: the predict pod publishes "latest" itself, and
# `scripts/launch_predict.sh publish` re-publishes it from the volume. This is for eras
# that live in their own OUT_DIR on the volume, and is the laptop fallback if a pod cannot
# reach MongoDB. Everything is staged in a temp dir that is deleted on exit — reports live
# in MongoDB only.
#
#   scripts/refresh_console.sh                     # derived/stage3 (+predict) -> Mongo bundle "latest"
#   scripts/refresh_console.sh stage3_h60 h60      # derived/stage3_h60          -> Mongo bundle "h60"
#
# The deployed API serves REPORT_BUNDLE (default "latest"); a named era is read by pointing
# a backend at it (REPORT_BUNDLE=h60), or via /api/predictions history once published.
set -euo pipefail
cd "$(dirname "$0")/.."
. data_acquisition/scripts/_common.sh

SRC_DIR="${1:-stage3}"                 # dir under /workspace/derived on the volume
BUNDLE="${2:-latest}"                  # bundle name in Mongo
TMP=$(mktemp -d -t console_bundle); trap 'rm -rf "$TMP"' EXIT
STAGE="$TMP/src"; OUT="$TMP/reports/$BUNDLE"; mkdir -p "$STAGE" "$OUT"
echo "volume derived/$SRC_DIR  ->  (temp)  ->  MongoDB bundle '$BUNDLE'"

# stage3 artifacts (book, trades, equity) + the ensemble stage's member/IC truth
aws s3 cp $S3FLAGS "$BUCKET/derived/$SRC_DIR/" "$STAGE/" --recursive \
  --exclude '*' --include 'stage3_*' --include '*.json'
for extra in stage2/stage2_report.json stage1/stage1_report.json \
             predict/suggestions.json predict/suggestions.md; do
  aws s3 cp $S3FLAGS "$BUCKET/derived/$extra" "$STAGE/" 2>/dev/null \
    || echo "  (optional $extra not on volume — that section is omitted)"
done
aws s3 cp $S3FLAGS "$BUCKET/m1/sessions.parquet" "$STAGE/" 2>/dev/null \
  || echo "  (m1/sessions.parquet not on volume — calendar omitted)"

python3 tools/build_reports.py --src "$STAGE" --out "$OUT"
if [ -f "$STAGE/suggestions.md" ]; then cp "$STAGE/suggestions.md" "$OUT/suggestions_latest.md"; fi
python3 tools/publish_mongo.py --bundle "$OUT" --name "$BUNDLE"
