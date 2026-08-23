#!/usr/bin/env bash
# [ONE-OFF from the initial overnight build — runs stages LOCALLY, predating
# the RunPod-first rule. The daily loop is scripts/daily.sh.]
# Overnight autonomy: watch stage2 GPU every 5 min with crash-loop/stall
# detection and ONE auto-relaunch; on success run stage3 + predict locally and
# assemble the final report. All output to the orchestrator log.
cd "$(dirname "$0")/.."
source data_acquisition/scripts/_common.sh
say() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

RELAUNCHED=0
LAST_LOG=""; LAST_SIZE=0; STALL_TICKS=0
while true; do
  sleep 300
  PODS=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
    -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null)
  RUNNING=$(printf '%s' "$PODS" | grep -c "predict-stage2" || true)
  ROW=$(aws s3 ls $S3FLAGS "$BUCKET/_pod_logs/" 2>/dev/null | grep predict-stage2 | tail -1)
  LOG=$(printf '%s' "$ROW" | awk '{print $4}')
  SIZE=$(printf '%s' "$ROW" | awk '{print $3}')
  BODY=$(aws s3 cp $S3FLAGS "$BUCKET/_pod_logs/$LOG" - 2>/dev/null | tail -6)

  if echo "$BODY" | grep -q "job=0"; then
    say "STAGE2 SUCCEEDED ($LOG)"; break
  fi
  if echo "$BODY" | grep -qE "job=[0-9]"; then
    say "STAGE2 FAILED ($LOG):"; echo "$BODY"
    if [ "$RELAUNCHED" = "0" ]; then
      RELAUNCHED=1
      say "auto-relaunching once..."
      for i in $(seq 1 30); do
        OUT=$(scripts/launch_predict.sh stage2 2>&1 | tail -2)
        echo "$OUT" | grep -qE "launched predict-" && { say "relaunched (attempt $i)"; break; }
        sleep 120
      done
      LAST_LOG=""; STALL_TICKS=0
      continue
    fi
    say "second failure — stopping; manual review needed"; exit 1
  fi
  # crash-loop: many fresh logs for one pod id within the last cycle
  N_RECENT=$(aws s3 ls $S3FLAGS "$BUCKET/_pod_logs/" 2>/dev/null | grep predict-stage2 \
    | awk -v d="$(date -u -v-15M +%Y-%m-%d)" '$1 >= d' | wc -l | tr -d ' ')
  if echo "$BODY" | grep -q "RESTART DETECTED"; then
    say "restart marker fired — pod terminating itself; watching"
  fi
  # stall detection: log byte-size unchanged for 18 ticks (90 min) while running
  if [ "$LOG" = "$LAST_LOG" ] && [ "$SIZE" = "$LAST_SIZE" ]; then
    STALL_TICKS=$((STALL_TICKS + 1))
  else
    STALL_TICKS=0
  fi
  LAST_LOG="$LOG"; LAST_SIZE="$SIZE"
  if [ "$RUNNING" = "0" ] && [ -n "$LOG" ]; then
    say "no pod running but no job= line — pod vanished; treating as failure"
    if [ "$RELAUNCHED" = "0" ]; then
      RELAUNCHED=1
      for i in $(seq 1 30); do
        OUT=$(scripts/launch_predict.sh stage2 2>&1 | tail -2)
        echo "$OUT" | grep -qE "launched predict-" && { say "relaunched"; break; }
        sleep 120
      done
      continue
    fi
    say "vanished twice — stopping"; exit 1
  fi
  if [ "$STALL_TICKS" -ge 18 ]; then
    say "WARN: log static for 90+ min (long training phase or hang) — continuing to watch"
    STALL_TICKS=0
  fi
  say "ok: pod=$RUNNING log=$LOG size=$SIZE"
done

# ---------- finalization ----------
say "downloading stage2 artifacts..."
mkdir -p derived_stage2
aws s3 cp $S3FLAGS "$BUCKET/derived/stage2/" derived_stage2/ --recursive \
  --exclude "*" --include "stage2_report.json" --include "scores_*.parquet" \
  --include "ensemble_rank.parquet" --include "daily_net_15bps.parquet" >/dev/null 2>&1
ls derived_stage2/ | head

say "running stage3 on the 7-member scores (local, no CPCV)..."
python3 -m src.pipeline.stage3 --m1 /tmp/real_m1 --eod /tmp/real_m1 \
  --out /tmp/stage3_stage2 --scores derived_stage2 --market /tmp/real_m1x \
  --no-cpcv > /tmp/stage3_stage2.log 2>&1
say "stage3 exit $? — tail:"
grep -E "M11-02|adoption|dsr" /tmp/stage3_stage2.log | tail -3

say "refreshing predictions..."
python3 -m src.pipeline.predict --m1 /tmp/real_m1 --eod /tmp/real_m1 \
  --out /tmp/predict_final --market /tmp/real_m1x > /tmp/predict_final.log 2>&1
say "predict exit $?"
cp /tmp/predict_final/suggestions.md artifacts/reports/suggestions_latest.md 2>/dev/null
cp /tmp/predict_final/suggestions.json artifacts/reports/suggestions_latest.json 2>/dev/null
cp /tmp/stage3_stage2/stage3_report.json artifacts/reports/stage3_stage2_report.json 2>/dev/null
cp derived_stage2/stage2_report.json artifacts/reports/ 2>/dev/null
say "ORCHESTRATION COMPLETE"
