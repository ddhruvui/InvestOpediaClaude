#!/usr/bin/env bash
# THE one-command daily loop for the MODELS. The vendor fetch is not here any more: the
# DataAcquistion repo (../DataAcquistion — its own scripts/daily.sh) fills the RunPod
# network volume and rebuilds the m1 tables; this script reads what it left behind and
# never launches a fetcher; everything it writes lands under results/InvestOpediaClaude/ on the
# volume (scripts/_common.sh RESULTS). Sequenced on self-terminating pods:
#
#   1. gate: the volume must be CURRENT — m1/_manifest.json written AFTER the newest
#      eod_bulk day-file (post consumed the latest pull) and no older than M1_MAX_AGE_H
#   2. m1x whole-market update (resumable — re-parses current year only)
#   3. predict (continual: warm-updates stored champions; full refit on cadence),
#      which then publishes the console bundle from the volume to MongoDB itself
#   4. confirm that publish landed (the deployed console reads Mongo; nothing is
#      kept on this machine)
#
# A REAPER runs in the background for the whole run (scripts/reap_pods.sh): pods cannot
# be trusted to delete themselves, and a survivor bills, restarts its job, and blocks its
# own relaunch as "already running".
#
# Everything is incremental on a warm volume, so a normal evening run is short.
# Safe to re-run: every launcher skips a stage whose pod is already up.
#
#   scripts/daily.sh                    # once the DataAcquistion fetch reports DONE
#   SKIP_DATA_CHECK=1 scripts/daily.sh  # run on whatever the volume holds (stale m1 allowed)
#   M1_MAX_AGE_H=48 scripts/daily.sh    # gate tolerance (default 36 h)
#   REFIT=full scripts/daily.sh         # force from-scratch model refit
#   NO_REAPER=1 scripts/daily.sh        # do not reap finished pods (debugging)
set -u
cd "$(dirname "$0")/.."
source scripts/_common.sh
say() { echo "[$(date -u +%H:%M:%SZ)] daily: $*"; }

# --reap-failed because a failed pod that stays up BLOCKS its own relaunch:
# launch_predict.sh skips a job whose pod name is already running, so leaving one
# alive would cost us the retry as well as the money.
REAPER_PID=""
stop_reaper() { [ -n "$REAPER_PID" ] && kill "$REAPER_PID" 2>/dev/null; return 0; }
trap stop_reaper EXIT INT TERM
if [ -z "${NO_REAPER:-}" ] && [ -z "${DRY_RUN:-}" ]; then
  scripts/reap_pods.sh --watch --reap-failed &
  REAPER_PID=$!
  say "0/4 reaper: watching pods (pid $REAPER_PID) — finished pods are deleted from here"
fi

say "1/4 gate: is the volume current? (the hand-off from the DataAcquistion repo)"
# `aws s3 ls` prints LastModified in LOCAL time for both objects, so the comparison is
# timezone-free; the age check compares against the local clock for the same reason.
MSTAMP=$(aws s3 ls $S3FLAGS "$BUCKET/m1/_manifest.json" 2>/dev/null | awk '{print $1" "$2}')
NEWEST=$(aws s3 ls $S3FLAGS "$BUCKET/data/eod_bulk/US/" 2>/dev/null | grep '\.json$' | sort -k4 | tail -1)
DSTAMP=$(printf '%s' "$NEWEST" | awk '{print $1" "$2}'); DFILE=$(printf '%s' "$NEWEST" | awk '{print $4}')
say "   m1/_manifest.json: ${MSTAMP:-missing}   newest eod_bulk day-file: ${DFILE:-none} (written ${DSTAMP:-?})"
GATE_OK=1
[ -n "$MSTAMP" ] || GATE_OK=""
if [ -n "$MSTAMP" ] && [ -n "$DSTAMP" ] && [ "$MSTAMP" \< "$DSTAMP" ]; then
  say "   !! m1 is OLDER than the newest day-file — post has not consumed the latest pull"; GATE_OK=""
fi
if [ -n "$MSTAMP" ]; then
  AGE_H=$(python3 -c 'import sys,datetime as d; m=d.datetime.strptime(sys.argv[1],"%Y-%m-%d %H:%M:%S"); print(int((d.datetime.now()-m).total_seconds()//3600))' "$MSTAMP")
  if [ "$AGE_H" -gt "${M1_MAX_AGE_H:-36}" ]; then
    say "   !! m1 manifest is ${AGE_H} h old (limit ${M1_MAX_AGE_H:-36} h) — no fresh fetch has landed"; GATE_OK=""
  fi
fi
if [ -z "$GATE_OK" ] && [ -z "${SKIP_DATA_CHECK:-}" ]; then
  say "FATAL: the volume is not current — the models would score stale tables."
  say "  run the fetch first:  (cd ../DataAcquistion && scripts/daily.sh)   or SKIP_DATA_CHECK=1 to override"
  exit 1
fi
[ -z "$GATE_OK" ] && say "   gate FAILED but SKIP_DATA_CHECK=1 — continuing on the volume as-is"
[ -n "$GATE_OK" ] && say "   gate OK — models will price against ${DFILE%.json}"

say "2/4 market: m1x whole-market update (resumable)"
export WATCH_SINCE=$(date -u +%Y%m%dT%H%M%SZ)
scripts/launch_predict.sh market
if [ -z "${DRY_RUN:-}" ]; then
  scripts/watch_jobs.sh market || { say "FATAL: market failed twice"; exit 1; }
fi

say "3/4 predict: continual warm update (REFIT=${REFIT:-auto}); the pod publishes to MongoDB itself"
export WATCH_SINCE=$(date -u +%Y%m%dT%H%M%SZ)
scripts/launch_predict.sh predict
if [ -z "${DRY_RUN:-}" ]; then
  scripts/watch_jobs.sh predict || { say "FATAL: predict failed twice"; exit 1; }
fi

# The predict pod is the last one up; reap it now rather than waiting on the background
# poll, so daily.sh never returns with a pod still billing.
[ -z "${NO_REAPER:-}" ] && [ -z "${DRY_RUN:-}" ] && \
  scripts/reap_pods.sh --reap-failed || true

[ -n "${DRY_RUN:-}" ] && { say "DRY_RUN: skipping publish check"; exit 0; }

say "4/4 publish: confirming the predict pod pushed the bundle to MongoDB"
# The pod publishes itself (pod_bootstrap_predict.sh publish_bundle) and its log carries
# `publish=<ec>` after the `verify:` line. Nothing is mirrored to this machine — reports
# live in MongoDB only, and the deployed console (Render UI -> Vercel API) reads them there.
LOG=$(aws s3 ls $S3FLAGS "$RESULTS/_pod_logs/" 2>/dev/null | awk '{print $4}' \
      | grep "predict-predict-" | sort | tail -1)
TAIL=$(aws s3 cp $S3FLAGS "$RESULTS/_pod_logs/$LOG" - 2>/dev/null \
       | grep -E '^(publish|verify:|job=)' | tail -4)
printf '%s\n' "$TAIL" | sed 's/^/  /'
if printf '%s\n' "$TAIL" | grep -q '^publish=0'; then
  say "DONE — MongoDB updated ($(printf '%s\n' "$TAIL" | grep -o 'as_of_close=[0-9-]*' | tail -1)); the deployed console is current"
else
  say "FATAL: predict finished but the publish did not — the deployed console still shows the previous run"
  say "  read the pod log ($LOG), fix the cause, then re-publish from the volume: scripts/launch_predict.sh publish"
  exit 1
fi
