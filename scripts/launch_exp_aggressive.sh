#!/usr/bin/env bash
# Launch an aggressive-branch experiment round (JOB=exp) on the EXPERIMENT
# volume. The production volume is untouched: code, scores and outputs all live
# on $EXP_VOLUME_ID (run scripts/launch_sync_volume.sh once first).
#   scripts/launch_exp_aggressive.sh <round-name> [variants.json]
# Round outputs land at /workspace/derived/exp_aggr/<round-name> on the volume.
set -euo pipefail
ROUND="${1:?usage: launch_exp_aggressive.sh <round-name> [variants.json]}"
VARIANTS_FILE="${2:-}"

export RUNPOD_VOLUME_ID_OVERRIDE="${EXP_VOLUME_ID:-crimtr8kbf}"
export OUT_DIR="/workspace/derived/exp_aggr/${ROUND}"
if [ -n "$VARIANTS_FILE" ]; then
  [ -f "$VARIANTS_FILE" ] || { echo "no such variants file: $VARIANTS_FILE" >&2; exit 2; }
  python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$VARIANTS_FILE" \
    || { echo "variants file is not valid JSON" >&2; exit 2; }
  VARIANTS_B64="$(base64 < "$VARIANTS_FILE" | tr -d '\n')"
  export VARIANTS_B64
fi
exec "$(dirname "$0")/launch_predict.sh" exp
