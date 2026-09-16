#!/usr/bin/env bash
# Grow the RunPod network volume by N GB (default 1). Sizes only go UP (RunPod rule), so this
# is deliberately incremental: the intraday fetcher stops with exit 75 when the volume is nearly
# full, the runner calls this once, relaunches, and repeats — "add 1 GB at a time, as needed".
#   scripts/grow_volume.sh        # +1 GB
#   scripts/grow_volume.sh 2      # +2 GB
# Prints the old and new size. Exit 1 if the API refuses or the size does not update within 90s.
. "$(dirname "$0")/_common.sh"
: "${RUNPOD_API_KEY:?account rpa_ key, set in runpod/.env}"
STEP="${1:-1}"
UA="investopediaclaude-grow/1.0"       # Cloudflare 403s the default Python/curl-less UA; see bootstrap.sh
cur=$(curl -sS "https://rest.runpod.io/v1/networkvolumes/$RUNPOD_VOLUME_ID" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "User-Agent: $UA" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["size"]))')
new=$((cur + STEP))
echo "volume $RUNPOD_VOLUME_ID: $cur GB -> $new GB"
resp=$(curl -sS -w $'\n%{http_code}' -X PATCH "https://rest.runpod.io/v1/networkvolumes/$RUNPOD_VOLUME_ID" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "User-Agent: $UA" -H 'Content-Type: application/json' \
  -d "{\"size\": $new}")
code=$(printf '%s' "$resp" | tail -n1)
if [ "$code" != "200" ] && [ "$code" != "201" ]; then
  echo "grow failed (HTTP $code): $(printf '%s' "$resp" | sed '$d')" >&2; exit 1
fi
for i in $(seq 1 9); do
  sleep 10
  now=$(curl -sS "https://rest.runpod.io/v1/networkvolumes/$RUNPOD_VOLUME_ID" \
    -H "Authorization: Bearer $RUNPOD_API_KEY" -H "User-Agent: $UA" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["size"]))')
  if [ "$now" -ge "$new" ]; then echo "volume now $now GB"; exit 0; fi
done
echo "size did not update within 90s (still $now GB)" >&2; exit 1
