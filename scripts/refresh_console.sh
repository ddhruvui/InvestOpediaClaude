#!/usr/bin/env bash
# Pull pod artifacts off the volume and build a console report bundle from them.
#
#   scripts/refresh_console.sh                     # stage3 -> derived/ -> reports/latest
#   scripts/refresh_console.sh stage3_h60 h60      # a named era -> derived_h60/ -> reports/h60
#
# Each pipeline run writes its own OUT_DIR on the volume
# (OUT_DIR=/workspace/derived/<era> scripts/launch_predict.sh stage3), and each
# era builds into its own reports/<bundle> here — so a new run never overwrites
# the numbers of the era you want to hold it against.
set -euo pipefail
cd "$(dirname "$0")/.."
. data_acquisition/scripts/_common.sh

SRC_DIR="${1:-stage3}"                 # dir under /workspace/derived on the volume
BUNDLE="${2:-latest}"                  # dir under reports/
LOCAL="derived_${SRC_DIR#stage3}"      # stage3 -> derived, stage3_h60 -> derived_h60
[ "$SRC_DIR" = "stage3" ] && LOCAL="derived"
LOCAL="${LOCAL//__/_}"; LOCAL="${LOCAL%_}"

echo "volume derived/$SRC_DIR  ->  $LOCAL/  ->  reports/$BUNDLE/"
mkdir -p "$LOCAL"

# stage3 artifacts (book, trades, equity) + the ensemble stage's member/IC truth
aws s3 cp $S3FLAGS "$BUCKET/derived/$SRC_DIR/" "$LOCAL/" --recursive \
  --exclude '*' --include 'stage3_*' --include '*.json'
for extra in stage2/stage2_report.json stage1/stage1_report.json \
             predict/suggestions.json; do
  aws s3 cp $S3FLAGS "$BUCKET/derived/$extra" "$LOCAL/" 2>/dev/null \
    || echo "  (optional $extra not on volume — bundle will omit that section)"
done

python3 tools/build_reports.py --src "$LOCAL" --out "reports/$BUNDLE"

python3 - "reports/$BUNDLE/summary.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
b = d.get("book", {})
print(f"bundle: config {d['stamp']['config_hash'][:12]} | snapshot "
      f"{d['stamp']['data_snapshot_id']} | verdict {d.get('gates',{}).get('verdict')}")
print(f"        Sharpe {b.get('sharpe_net')} | MDD {b.get('mdd')} | "
      f"trades {b.get('n_trades')}")
PY
