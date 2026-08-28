#!/usr/bin/env bash
# Pull the run's artifacts off the volume into ./derived, enforce the G-02 freshness
# guard, and rebuild reports/latest for the UI.
#
# This is the ONLY thing that has to run after predict. Do NOT reach for scripts/daily.sh
# here — that re-runs the whole pipeline (fetch, post, market, predict) and would redo an
# hour of work to accomplish a two-minute copy.
#
#   FULL_MIRROR=1   also re-pull the stage-3 parquets (MBs; only change on a stage3 rerun)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$(cd "$HERE/../../../.." && pwd)"
say() { echo "[$(date -u +%H:%M:%SZ)] mirror: $*"; }
mirror() { "$HERE/vol" cp "$1" "$2" --quiet >/dev/null 2>&1; }

mkdir -p derived

# The suggestions book is the one artifact the run exists to produce.
mirror derived/predict/suggestions.json derived/suggestions.json \
  || { say "FATAL: no suggestions.json on the volume — did predict actually finish?"; exit 1; }

# G-02. EODHD publishes the bulk day-file ~19:30 ET, so a chain that built m1 before the
# fetch finished silently scores the PRIOR close and everything downstream looks normal.
# Comparing the book's as_of_close against the newest day-file is what catches that.
AS_OF=$(python3 -c "import json;print(json.load(open('derived/suggestions.json'))['as_of_close'])")
LATEST=$("$HERE/vol" ls data/eod_bulk/US/ | awk '{print $4}' \
         | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.json$' | sort | tail -1)
LATEST="${LATEST%.json}"
if [ -n "$LATEST" ] && [ "$AS_OF" != "$LATEST" ]; then
  say "FATAL: as_of_close=$AS_OF but newest day-file is $LATEST"
  say "  the m1/market/predict chain ran before today's data landed — rerun post, market, predict"
  exit 1
fi
say "G-02 OK: as_of_close=$AS_OF matches newest day-file $LATEST"

mirror derived/predict/suggestions.md reports/suggestions_latest.md || true
mirror m1/sessions.parquet derived/sessions.parquet || true

# Stage reports are small JSON and cheap to refresh; they only change when a stage reruns.
for f in stage1/stage1_report.json stage2/stage2_report.json \
         stage3/stage3_report.json stage3/stage3_final_report.json; do
  mirror "derived/$f" "derived/$(basename "$f")" || true
done

# Stage-3 parquets are MBs and only change on a stage3 rerun: pull when absent or forced.
for f in stage3_equity.parquet stage3_daily_net.parquet stage3_trades_ungated.parquet; do
  if [ -n "${FULL_MIRROR:-}" ] || [ ! -f "derived/$f" ]; then
    mirror "derived/stage3/$f" "derived/$f" || say "note: $f not on volume (stage3 not run yet)"
  fi
done

say "rebuilding reports/latest"
python3 tools/build_reports.py --src derived --out reports/latest || {
  say "FATAL: build_reports failed"; exit 1; }
say "DONE — serve with: (cd app/backend && npm start)  ->  http://localhost:8787"
