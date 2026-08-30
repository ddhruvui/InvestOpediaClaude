#!/usr/bin/env bash
# One-shot data-sync pod (branch aggressive-short-horizon): mounts the
# EXPERIMENT volume at /workspace and copies the prediction-stack inputs from
# the SOURCE volume over the S3 API — read-only GETs against the source, which
# is never written. `aws s3 sync` makes re-runs resumable.
# HARD RULE (same as pod_bootstrap_predict.sh): control ALWAYS reaches the
# self-termination block; a restart hits the marker and terminates.
set +e
BOOT_LOG_DIR="/workspace/_pod_logs"
mkdir -p "$BOOT_LOG_DIR" 2>/dev/null
BOOT_LOG="$BOOT_LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-sync-${RUNPOD_POD_ID:-nopod}.log"
exec > >(tee -a "$BOOT_LOG") 2>&1
echo "sync bootstrap $(date -u +%FT%TZ) pod=${RUNPOD_POD_ID:-?} src=${SRC_VOLUME_ID:-?}"

MARKER="/workspace/_pod_logs/.ran-${RUNPOD_POD_ID:-nopod}"
ec=98
if [ -f "$MARKER" ]; then
  echo "RESTART DETECTED (marker exists) — skipping sync, terminating"
elif [ -z "${SRC_VOLUME_ID:-}" ] || [ -z "${RUNPOD_S3_ENDPOINT:-}" ]; then
  echo "FATAL: SRC_VOLUME_ID / RUNPOD_S3_ENDPOINT unset"; ec=97
else
  touch "$MARKER" 2>/dev/null
  timeout 600 python -m pip install --quiet --no-input --disable-pip-version-check awscli \
    || echo "!! pip awscli failed"
  SRC="s3://${SRC_VOLUME_ID}"
  EP=(--endpoint-url "$RUNPOD_S3_ENDPOINT" --region "${RUNPOD_S3_REGION:-eu-ro-1}")
  ec=0
  if [ "${SYNC_SET:-inputs}" = "raw" ]; then
    # RAW MODE (adoption rebuild): copy ONLY the downloaded vendor trees +
    # the DSR trials ledger. Everything derived (m1, m1x, scores, models)
    # is rebuilt from scratch on this volume by the pipeline itself.
    for tree in data data_nasdaq data_tiingo data_borrow data_calendar                 data_finbert data_quality ledger; do
      echo ">> $SRC/$tree/ -> /workspace/$tree/"
      timeout 14400 aws s3 sync "${EP[@]}" --only-show-errors \
        "$SRC/$tree" "/workspace/$tree" --exclude 'logs/*' || ec=1
    done
    echo "---- volume contents after raw sync ----"
    du -sh /workspace/* 2>/dev/null
    echo "sync done ec=$ec at $(date -u +%FT%TZ)"
    sync 2>/dev/null
    [ "${KEEP_POD:-}" = "1" ] && { echo "KEEP_POD=1 — not terminating"; sleep infinity; }
  else
  echo ">> $SRC/m1/ -> /workspace/m1/"
  timeout 3600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/m1" /workspace/m1 || ec=1
  echo ">> $SRC/m1x/ -> /workspace/m1x/"
  timeout 14400 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/m1x" /workspace/m1x || ec=1
  echo ">> $SRC/data/market/ -> /workspace/data/market/"
  timeout 3600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/data/market" /workspace/data/market || ec=1
  echo ">> $SRC/derived/stage2/ (scores + sentiment) -> /workspace/derived/stage2/"
  timeout 7200 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/derived/stage2" /workspace/derived/stage2 \
    --exclude '*' --include 'scores_*.parquet' --include 'sentiment_scores.parquet' \
    --include 'stage2_report.json' || ec=1
  echo ">> $SRC/derived/stage1/ (scores) -> /workspace/derived/stage1/"
  timeout 3600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/derived/stage1" /workspace/derived/stage1 \
    --exclude '*' --include 'scores_*.parquet' || ec=1
  echo ">> $SRC/ledger/ -> /workspace/ledger/ (seed DSR trial count)"
  timeout 600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/ledger" /workspace/ledger || ec=1
  echo "---- volume contents after sync ----"
  du -sh /workspace/* 2>/dev/null
  echo "sync done ec=$ec at $(date -u +%FT%TZ)"
  fi
fi
sync 2>/dev/null

[ "${KEEP_POD:-}" = "1" ] && { echo "KEEP_POD=1 — not terminating"; sleep infinity; }
for attempt in $(seq 1 12); do
  timeout 60 python - <<'PY'
import os, ssl, sys, urllib.error, urllib.request as u
pid = os.environ.get("RUNPOD_POD_ID", ""); key = os.environ.get("RUNPOD_TERMINATE_KEY", "")
if not pid or not key: sys.exit(2)
url = "https://rest.runpod.io/v1/pods/" + pid
for ctx in (None, ssl._create_unverified_context()):
    try:
        req = u.Request(url, method="DELETE"); req.add_header("Authorization", "Bearer " + key)
        st = u.urlopen(req, timeout=30, context=ctx).status
        if st == 204: print("terminated (204)"); sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 404: print("already gone (404)"); sys.exit(0)
    except Exception as e:
        print("terminate error:", e, file=sys.stderr)
sys.exit(1)
PY
  [ $? -eq 0 ] && exit 0
  echo "terminate attempt $attempt not confirmed — retry in 20s"; sleep 20
done
echo "!! TERMINATION NOT CONFIRMED — run data_acquisition/scripts/killpod.sh"
sleep 30
