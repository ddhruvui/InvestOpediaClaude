#!/usr/bin/env bash
# [ONE-OFF from the initial overnight build — runs stages LOCALLY, predating
# the RunPod-first rule. The daily loop is scripts/daily.sh.]
# Finalization chain: wait for local stage3 -> predict refresh -> final report.
# Prints a status line every 10 min; on any step failure prints the log tail
# and stops so the failure is visible and fixable.
cd "$(dirname "$0")/.."
say() { echo "[$(date -u +%H:%M:%SZ)] $*"; }

# ---- 1. wait for stage3 ----
for i in $(seq 1 60); do
  if [ -f /tmp/stage3_final/stage3_report.json ]; then
    say "stage3 report present"; break
  fi
  if ! pgrep -f "src.pipeline.stage3" >/dev/null; then
    if [ -f /tmp/stage3_final/stage3_report.json ]; then break; fi
    say "STAGE3 DIED without a report — log tail:"
    tail -20 /tmp/stage3_final.log | grep -vE "Warning|warn"
    exit 1
  fi
  [ $((i % 5)) -eq 0 ] && say "stage3 still running ($(pgrep -f src.pipeline.stage3 | head -1))"
  sleep 120
done
[ -f /tmp/stage3_final/stage3_report.json ] || { say "stage3 timeout"; exit 1; }
say "stage3 summary:"
python3 -c "
import json
r = json.load(open('/tmp/stage3_final/stage3_report.json'))
a = r['adoption']
for k in ('ungated','gated'):
    b = a[k]
    print(f\"  {k}: sharpe {b['sharpe']:.3f} trades {b['n_trades']} mdd {b['mdd']:.3f}\")
print('  adopt:', a['adopt'], '| DSR:', round(r['dsr']['DSR'],4))"

# ---- 2. predictions refresh ----
say "refreshing predictions..."
python3 -m src.pipeline.predict --m1 /tmp/real_m1 --eod /tmp/real_m1 \
  --out /tmp/predict_final --market /tmp/real_m1x > /tmp/predict_final.log 2>&1
if [ $? -ne 0 ]; then
  say "PREDICT FAILED — log tail:"; tail -15 /tmp/predict_final.log; exit 1
fi
say "predict done: $(grep 'suggestions written' /tmp/predict_final.log)"

# ---- 3. collect artifacts ----
mkdir -p artifacts/reports
cp /tmp/predict_final/suggestions.md artifacts/reports/suggestions_latest.md
cp /tmp/predict_final/suggestions.json artifacts/reports/suggestions_latest.json
cp /tmp/stage3_final/stage3_report.json artifacts/reports/stage3_ensemble_report.json
cp derived_stage2/stage2_report.json artifacts/reports/ 2>/dev/null
say "FINALIZATION COMPLETE — artifacts in artifacts/reports/"
