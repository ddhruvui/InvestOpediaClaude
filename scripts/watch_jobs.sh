#!/usr/bin/env bash
# Watch pod jobs to completion (bash 3.2 compatible — no associative arrays).
# Tolerates transient network errors; one auto-relaunch per job on failure.
# Usage: watch_jobs.sh job1 [job2]
cd "$(dirname "$0")/.."
source data_acquisition/scripts/_common.sh
say() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

check_job() {  # $1=job  $2=relaunched-flag-file ; echo status: running|done|failed
  local j="$1" flag="$2" ROW LOG TAIL
  ROW=$(aws s3 ls $S3FLAGS "$BUCKET/_pod_logs/" 2>/dev/null | grep "predict-$j-" | tail -1)
  if [ -z "$ROW" ]; then say "WARN: cannot list logs for $j"; echo running; return; fi
  LOG=$(printf '%s' "$ROW" | awk '{print $4}')
  TAIL=$(aws s3 cp $S3FLAGS "$BUCKET/_pod_logs/$LOG" - 2>/dev/null | tail -5)
  if echo "$TAIL" | grep -q "job=0"; then say "$j SUCCEEDED ($LOG)"; echo done; return; fi
  if echo "$TAIL" | grep -qE "job=[0-9]"; then
    say "$j FAILED ($LOG): $(echo "$TAIL" | head -1 | cut -c1-100)"
    if [ ! -f "$flag" ]; then
      touch "$flag"; say "auto-relaunching $j once..."
      local i OUT
      for i in $(seq 1 30); do
        OUT=$(RUNPOD_VCPU=8 scripts/launch_predict.sh "$j" 2>&1 | tail -2)
        echo "$OUT" | grep -qE "launched predict-|already running" && { say "$j relaunched"; break; }
        sleep 120
      done
      echo running; return
    fi
    say "$j failed twice — needs manual review"; echo failed; return
  fi
  say "$j running: $LOG ($(printf '%s' "$ROW" | awk '{print $3}') bytes)"
  echo running
}

J1="$1"; J2="${2:-}"
F1=$(mktemp -t relaunch1); rm -f "$F1"
F2=$(mktemp -t relaunch2); rm -f "$F2"
S1=running; S2=done
[ -n "$J2" ] && S2=running
while true; do
  [ "$S1" = "running" ] && S1=$(check_job "$J1" "$F1" | tail -1)
  [ -n "$J2" ] && [ "$S2" = "running" ] && S2=$(check_job "$J2" "$F2" | tail -1)
  if [ "$S1" != "running" ] && [ "$S2" != "running" ]; then
    say "ALL JOBS FINISHED: $J1=$S1 ${J2:+$J2=$S2}"; break
  fi
  sleep 600
done
[ "$S1" = "done" ] && { [ -z "$J2" ] || [ "$S2" = "done" ]; }
