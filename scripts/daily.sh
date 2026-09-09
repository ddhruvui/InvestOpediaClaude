#!/usr/bin/env bash
# THE one-command daily loop. Sequences the whole day on self-terminating pods:
#
#   1. vendor fetch (launch.sh all) + post (validate -> build_m1), which
#      self-sequences on today's vendor manifests
#   2. m1x whole-market update (resumable — re-parses current year only)
#   3. predict (continual: warm-updates stored champions; full refit on cadence),
#      which then publishes the console bundle from the volume to MongoDB itself
#   4. confirm that publish landed (the deployed console reads Mongo; nothing is
#      kept on this machine)
#
# A REAPER runs in the background for the whole run. Pods are supposed to delete
# themselves, but on 2026-09-09 they all silently stopped being able to (a Cloudflare
# User-Agent block) and sat billing — and, worse, RunPod relaunched them, so the
# fetchers re-ran every 5 minutes and the next night's `launch.sh all` skipped those
# vendors as "already running". The reaper reads each pod's own log off the volume and
# deletes it once its job has finished, so the run no longer depends on that at all.
#
# Everything is incremental on a warm volume, so a normal evening run is short.
# Safe to re-run: every launcher skips a stage whose pod is already up.
#
#   scripts/daily.sh                    # start ~22:00 UTC (18:00 ET); EODHD's bulk
#                                       # day-file lands ~23:30 UTC, and the fetch
#                                       # should finish before 00:00 UTC
#   SKIP_FETCH=1 scripts/daily.sh       # data already fetched today
#   REFIT=full scripts/daily.sh         # force from-scratch model refit
#   NO_REAPER=1 scripts/daily.sh        # do not reap finished pods (debugging)
set -u
cd "$(dirname "$0")/.."
source data_acquisition/scripts/_common.sh
say() { echo "[$(date -u +%H:%M:%SZ)] daily: $*"; }

# Background reaper for the whole run. --reap-failed because a failed pod that stays up
# BLOCKS its own relaunch: launch.sh and launch_predict.sh both skip a job whose pod name
# is already running, so leaving one alive would cost us the retry as well as the money.
REAPER_PID=""
stop_reaper() { [ -n "$REAPER_PID" ] && kill "$REAPER_PID" 2>/dev/null; return 0; }
trap stop_reaper EXIT INT TERM
if [ -z "${NO_REAPER:-}" ] && [ -z "${DRY_RUN:-}" ]; then
  data_acquisition/scripts/reap_pods.sh --watch --reap-failed &
  REAPER_PID=$!
  say "0/4 reaper: watching pods (pid $REAPER_PID) — finished pods are deleted from here"
fi

# EODHD's bulk day-file must not be pulled mid-session — a partial file would be
# frozen forever (the fetcher never re-pulls existing day-files)
if [ -z "${SKIP_FETCH:-}" ] && [ -z "${SKIP_TIME_CHECK:-}" ] && [ "$(date -u +%H)" -lt 21 ]; then
  echo "!! before 21:00 UTC — the EODHD day-file may be mid-session." >&2
  echo "   SKIP_TIME_CHECK=1 to override, SKIP_FETCH=1 to skip the fetch stage." >&2
  exit 2
fi

if [ -z "${SKIP_FETCH:-}" ]; then
  say "1/4 fetch: vendor pods + post (post waits for today's manifests itself)"
  data_acquisition/scripts/launch.sh all
  data_acquisition/scripts/launch.sh post
  if [ -n "${DRY_RUN:-}" ]; then say "DRY_RUN: skipping post wait"; else
    TODAY=$(date -u +%Y-%m-%d)
    ok=""
    for i in $(seq 1 90); do    # poll 5-min, up to 7.5 h
      sleep 300
      PODS=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
        -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || \
        { say "WARN: pods API unreachable — retrying"; continue; }
      if printf '%s' "$PODS" | grep -q "investopediaclaude-post"; then
        say "post still running"; continue
      fi
      # post pod gone -> only trust it if today's m1 build actually landed
      MDATE=$(aws s3 ls $S3FLAGS "$BUCKET/m1/_manifest.json" 2>/dev/null | awk '{print $1}')
      if [ "$MDATE" = "$TODAY" ]; then say "post done — m1 rebuilt today"; ok=1; break; fi
      say "post pod gone but m1 manifest is '$MDATE' (not today) — waiting/retrying"
    done
    [ -n "$ok" ] || { say "FATAL: m1 was not rebuilt today — check launch.sh post logs"; exit 1; }
  fi
else
  say "1/4 fetch: SKIPPED (SKIP_FETCH=1)"
fi

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
  data_acquisition/scripts/reap_pods.sh --reap-failed || true

[ -n "${DRY_RUN:-}" ] && { say "DRY_RUN: skipping publish check"; exit 0; }

say "4/4 publish: confirming the predict pod pushed the bundle to MongoDB"
# The pod publishes itself (pod_bootstrap_predict.sh publish_bundle) and its log carries
# `publish=<ec>` after the `verify:` line. Nothing is mirrored to this machine — reports
# live in MongoDB only, and the deployed console (Render UI -> Vercel API) reads them there.
LOG=$(aws s3 ls $S3FLAGS "$BUCKET/_pod_logs/" 2>/dev/null | awk '{print $4}' \
      | grep "predict-predict-" | sort | tail -1)
TAIL=$(aws s3 cp $S3FLAGS "$BUCKET/_pod_logs/$LOG" - 2>/dev/null \
       | grep -E '^(publish|verify:|job=)' | tail -4)
printf '%s\n' "$TAIL" | sed 's/^/  /'
if printf '%s\n' "$TAIL" | grep -q '^publish=0'; then
  say "DONE — MongoDB updated ($(printf '%s\n' "$TAIL" | grep -o 'as_of_close=[0-9-]*' | tail -1)); the deployed console is current"
else
  say "FATAL: predict finished but the publish did not — the deployed console still shows the previous run"
  say "  read the pod log ($LOG), fix the cause, then re-publish from the volume: scripts/launch_predict.sh publish"
  exit 1
fi
