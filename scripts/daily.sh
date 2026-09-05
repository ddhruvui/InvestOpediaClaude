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
#   scripts/daily.sh                    # start ~22:00 UTC (18:00 ET); EODHD's bulk
#                                       # day-file lands ~23:30 UTC, and the fetch
#                                       # should finish before 00:00 UTC
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

say "3/5 predict: continual warm update (REFIT=${REFIT:-auto}); the pod publishes to MongoDB itself"
export WATCH_SINCE=$(date -u +%Y%m%dT%H%M%SZ)
scripts/launch_predict.sh predict
if [ -z "${DRY_RUN:-}" ]; then
  scripts/watch_jobs.sh predict || { say "FATAL: predict failed twice"; exit 1; }
fi

[ -n "${DRY_RUN:-}" ] && { say "DRY_RUN: skipping mirror + reports"; exit 0; }

say "4/5 mirror: pulling report inputs off the volume (local git record + fallback publish)"
mkdir -p derived
mirror() { aws s3 cp $S3FLAGS "$BUCKET/$1" "$2" >/dev/null 2>&1; }
mirror derived/predict/suggestions.json derived/suggestions.json \
  || { say "FATAL: no suggestions.json on the volume"; exit 1; }
# G-02 sanity: suggestions must be scored off the newest day-file on the volume.
# 2026-08-24 failure mode: EODHD publishes the BULK day-file ~19:30 ET, so a chain
# that builds m1 before the fetch finishes silently scores the PRIOR close.
AS_OF=$(python3 -c "import json; print(json.load(open('derived/suggestions.json'))['as_of_close'])")
LATEST_BULK=$({ aws s3 ls $S3FLAGS "$BUCKET/data/eod_bulk/US/" | awk '{print $4}' \
  | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.json$' | sort | tail -1; } || true)
LATEST_BULK="${LATEST_BULK%.json}"
if [ -n "$LATEST_BULK" ] && [ "$AS_OF" != "$LATEST_BULK" ]; then
  say "FATAL: suggestions as_of_close=$AS_OF but newest eod_bulk day-file is $LATEST_BULK —"
  say "  the m1/market/predict chain ran before today's data landed; re-run post, market, predict"
  exit 1
fi
say "verified: as_of_close=$AS_OF matches newest day-file"
[ "$LATEST_BULK" = "$(date -u +%F)" ] || say "WARN: newest day-file $LATEST_BULK is not today's UTC date (holiday/weekend, or EODHD bulk not yet published when the fetch ran)"
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
python3 tools/build_reports.py --src derived --out reports/latest || { say "FATAL: build_reports failed"; exit 1; }
# The deployed console (Vercel API + Render UI) reads MongoDB, not this disk:
# until this step runs it still shows the previous run. SKIP_PUBLISH=1 to skip.
if [ -z "${SKIP_PUBLISH:-}" ]; then
  say "5/5 publish: reports/latest -> MongoDB"
  python3 tools/publish_mongo.py --bundle reports/latest \
    || { say "FATAL: publish_mongo failed — the deployed console still shows the previous run"; exit 1; }
fi
say "DONE — deployed console reads MongoDB; locally: (cd app/backend && npm start), or read reports/suggestions_latest.md"
