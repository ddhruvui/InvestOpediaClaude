#!/usr/bin/env bash
# Retry launch_predict.sh until EU-RO-1 has capacity: scripts/launch_retry.sh <job> [vcpu] [attempts]
JOB="${1:?job}"; VCPU="${2:-8}"; N="${3:-60}"
cd "$(dirname "$0")/.."
for i in $(seq 1 "$N"); do
  OUT=$(RUNPOD_VCPU="$VCPU" scripts/launch_predict.sh "$JOB" 2>&1 | tail -3)
  if echo "$OUT" | grep -qE "launched predict-|already running"; then
    echo "OK (attempt $i, vcpu $VCPU):"; echo "$OUT"; exit 0
  fi
  # halve once if the big flavor never places
  [ "$i" = "20" ] && [ "$VCPU" -gt 4 ] && { VCPU=4; echo "downshifting to 4 vCPU"; }
  [ "$i" = "40" ] && [ "$VCPU" -gt 2 ] && { VCPU=2; echo "downshifting to 2 vCPU"; }
  echo "attempt $i ($VCPU vCPU): no capacity"; sleep 90
done
echo "gave up after $N attempts"; exit 1
