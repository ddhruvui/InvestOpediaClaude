#!/usr/bin/env bash
# THE one-command daily loop. Sequences the whole day on self-terminating pods:
#
#   1. vendor fetch (launch.sh all) + post (validate -> build_m1), which
#      self-sequences on today's vendor manifests
#   2. m1x whole-market update (resumable — re-parses current year only)
#   3. predict (continual: warm-updates stored champions; full refit on cadence)
#   4. mirror the report inputs off the volume
#   5. rebuild reports/latest for the console
#
# Everything is incremental on a warm volume, so a normal evening run is short.
# Safe to re-run: every launcher skips a stage whose pod is already up.
#
#   scripts/daily.sh                    # evenings after ~21:00 UTC (17:00 ET)
#   SKIP_FETCH=1 scripts/daily.sh       # data already fetched today
#   REFIT=full scripts/daily.sh         # force from-scratch model refit
#   FULL_MIRROR=1 scripts/daily.sh      # re-pull stage parquets (after stage3 rerun)
set -u
cd "$(dirname "$0")/.."
source data_acquisition/scripts/_common.sh
say() { echo "[$(date -u +%H:%M:%SZ)] daily: $*"; }

# EODHD's bulk day-file must not be pulled mid-session — a partial file would be
# frozen forever (the fetcher never re-pulls existing day-files)
if [ -z "${SKIP_FETCH:-}" ] && [ -z "${SKIP_TIME_CHECK:-}" ] && [ "$(date -u +%H)" -lt 21 ]; then
  echo "!! before 21:00 UTC — the EODHD day-file may be mid-session." >&2
  echo "   SKIP_TIME_CHECK=1 to override, SKIP_FETCH=1 to skip the fetch stage." >&2
  exit 2
fi

if [ -z "${SKIP_FETCH:-}" ]; then
  say "1/5 fetch: vendor pods + post (post waits for today's manifests itself)"
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
  say "1/5 fetch: SKIPPED (SKIP_FETCH=1)"
fi

say "2/5 market: m1x whole-market update (resumable)"
export WATCH_SINCE=$(date -u +%Y%m%dT%H%M%SZ)
scripts/launch_predict.sh market
if [ -z "${DRY_RUN:-}" ]; then
  scripts/watch_jobs.sh market || { say "FATAL: market failed twice"; exit 1; }
fi

say "3/5 predict: continual warm update (REFIT=${REFIT:-auto})"
export WATCH_SINCE=$(date -u +%Y%m%dT%H%M%SZ)
scripts/launch_predict.sh predict
if [ -z "${DRY_RUN:-}" ]; then
  scripts/watch_jobs.sh predict || { say "FATAL: predict failed twice"; exit 1; }
fi

[ -n "${DRY_RUN:-}" ] && { say "DRY_RUN: skipping mirror + reports"; exit 0; }

say "4/5 mirror: pulling report inputs off the volume"
mkdir -p derived
mirror() { aws s3 cp $S3FLAGS "$BUCKET/$1" "$2" >/dev/null 2>&1; }
mirror derived/predict/suggestions.json derived/suggestions.json \
  || { say "FATAL: no suggestions.json on the volume"; exit 1; }
mirror derived/predict/suggestions.md reports/suggestions_latest.md || true
mirror m1/sessions.parquet derived/sessions.parquet || true
# stage reports are tiny JSON — refresh daily; they only change when stages rerun
for f in stage1/stage1_report.json stage2/stage2_report.json \
         stage3/stage3_report.json stage3/stage3_final_report.json; do
  mirror "derived/$f" "derived/$(basename "$f")" || true
done
# stage-3 parquets (equity/trades) are MBs and only change on a stage3 rerun:
# pull when missing locally or when FULL_MIRROR=1
for f in stage3_equity.parquet stage3_daily_net.parquet stage3_trades_ungated.parquet; do
  if [ -n "${FULL_MIRROR:-}" ] || [ ! -f "derived/$f" ]; then
    mirror "derived/stage3/$f" "derived/$f" || say "note: derived/stage3/$f not on volume"
  fi
done

say "5/5 reports: rebuilding reports/latest bundle"
python3 tools/build_reports.py --src derived --out reports/latest
say "DONE — Today page: (cd app/backend && npm start), or read reports/suggestions_latest.md"
