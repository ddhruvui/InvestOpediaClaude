#!/usr/bin/env bash
# Prediction-stack pod entrypoint. HARD RULE: control ALWAYS reaches the
# self-termination block — a bare `exit` before it caused a crash-restart
# billing loop (RunPod restarts the container when dockerStartCmd exits).
# A per-pod marker on the volume makes restarts terminate instead of re-running.
set +e
BOOT_LOG_DIR="/workspace/_pod_logs"
mkdir -p "$BOOT_LOG_DIR" 2>/dev/null
BOOT_LOG="$BOOT_LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-predict-${JOB:-stage1}-${RUNPOD_POD_ID:-nopod}.log"
exec > >(tee -a "$BOOT_LOG") 2>&1
echo "predict bootstrap $(date -u +%FT%TZ) pod=${RUNPOD_POD_ID:-?} job=${JOB:-stage1}"

MARKER="/workspace/_pod_logs/.ran-${RUNPOD_POD_ID:-nopod}"
ec=98
if [ -f "$MARKER" ]; then
  echo "RESTART DETECTED (marker $MARKER exists) — skipping job, terminating"
else
  touch "$MARKER" 2>/dev/null
  python -c 'import sys; print("python", sys.version)'
  nvidia-smi 2>/dev/null | head -12 || echo "(no GPU)"

  WORKDIR="predict_src_${JOB:-stage1}"        # per-job dir: concurrent pods share the volume
  cd /workspace && rm -rf "$WORKDIR" && mkdir "$WORKDIR" && cd "$WORKDIR"
  if tar xzf /workspace/code/predict/bundle.tgz --no-same-owner -m; then
    echo "bundle unpacked: $(find . -name '*.py' | wc -l) py files"
    command -v apt-get >/dev/null && { apt-get update -qq && \
      apt-get install -y -qq libgomp1 >/dev/null 2>&1 || echo "!! libgomp1 install failed"; }
    PIP="${PIP_PACKAGES:-pandas pyarrow numpy PyYAML scipy lightgbm scikit-learn}"
    echo "pip install: $PIP"
    timeout 1200 python -m pip install --quiet --no-input --disable-pip-version-check $PIP \
      || echo "!! pip install failed — job will report missing imports"

    export M1_DIR="${M1_DIR:-/workspace/m1}"
    export EOD_DIR="${EOD_DIR:-/workspace/data}"
    export MARKET_DIR="${MARKET_DIR:-/workspace/m1x}"
    export OUT_DIR="${OUT_DIR:-/workspace/derived/${JOB:-stage1}}"
    # G-09: one ledger for ALL runs — a per-pod ledger would undercount DSR's N
    export LEDGER_PATH="${LEDGER_PATH:-/workspace/ledger/trials.parquet}"
    # continual learning: champions persist here between daily predict runs
    export MODEL_DIR="${MODEL_DIR:-/workspace/models}"
    export REFIT="${REFIT:-auto}"
    mkdir -p "$OUT_DIR"

    # Publish: build the console bundle from the volume and push it to MongoDB, so the
    # deployed site (Render UI -> Vercel API -> Mongo) updates without anything being
    # downloaded to a laptop. Runs after a successful predict, or as JOB=publish.
    publish_bundle() {
      echo "--- publish: volume -> reports bundle -> MongoDB ($(date -u +%FT%TZ))"
      [ -n "${MONGO_URI:-}" ] || { echo "publish: MONGO_URI not set — skipping"; return 3; }
      # G-02: the book must be scored off the NEWEST day-file. EODHD publishes the bulk
      # file late (~19:30 ET); a chain that built m1 before it landed scores the prior
      # close and every downstream number still looks plausible.
      timeout 300 python - <<'PY' || return 4
import json, os, re, sys
sug = "/workspace/derived/predict/suggestions.json"
try:
    as_of = json.load(open(sug))["as_of_close"]
except Exception as e:
    print(f"publish: cannot read {sug}: {e}"); sys.exit(1)
try:
    days = sorted(f[:-5] for f in os.listdir("/workspace/data/eod_bulk/US")
                  if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f))
except OSError as e:
    print(f"publish: cannot list day-files: {e}"); days = []
latest = days[-1] if days else None
if latest and as_of != latest:
    print(f"publish: G-02 FAIL as_of_close={as_of} but newest day-file is {latest} — "
          "refusing to publish a stale book; rerun post, market, predict"); sys.exit(1)
print(f"publish: G-02 OK as_of_close={as_of} newest day-file={latest}")
PY
      local BUNDLE_NAME="${REPORT_BUNDLE:-latest}" OUT="/workspace/reports/${REPORT_BUNDLE:-latest}"
      timeout 900 python tools/build_reports.py --volume /workspace --out "$OUT" || return 5
      timeout 600 python tools/publish_mongo.py --bundle "$OUT" --name "$BUNDLE_NAME" || return 6
      # keep the human-readable book next to the bundle on the volume
      cp -f /workspace/derived/predict/suggestions.md "$OUT/suggestions_latest.md" 2>/dev/null || true
    }

    case "${JOB:-stage1}" in
      test)    timeout 3600  python -m pytest tests/ -q ;;
      market)  timeout 28800 python src/data/build_market.py ;;
      stage1)  timeout 28800 python -m src.pipeline.stage1 --m1 "$M1_DIR" --eod "$EOD_DIR" \
                 --out "$OUT_DIR" ${USE_MARKET:+--market "$MARKET_DIR"} ;;
      stage2)  timeout 64800 python -m src.pipeline.stage2 --m1 "$M1_DIR" --eod "$EOD_DIR" \
                 --out "$OUT_DIR" ${USE_MARKET:+--market "$MARKET_DIR"} ;;
      stage3)  timeout 28800 python -m src.pipeline.stage3 --m1 "$M1_DIR" --eod "$EOD_DIR" \
                 --out "$OUT_DIR" --scores "${SCORES_DIR:-/workspace/derived/stage2}" \
                 ${USE_MARKET:+--market "$MARKET_DIR"} ${NO_CPCV:+--no-cpcv} ;;
      exp)     timeout 28800 python -m src.pipeline.experiments --m1 "$M1_DIR" \
                 --eod "$EOD_DIR" --out "$OUT_DIR" \
                 --scores "${SCORES_DIR:-/workspace/derived/stage2}" \
                 --scores-alt "${SCORES_DIR_ALT:-/workspace/derived/stage1}" \
                 ${USE_MARKET:+--market "$MARKET_DIR"} ;;
      predict) timeout 14400 python -m src.pipeline.predict --m1 "$M1_DIR" --eod "$EOD_DIR" \
                 --out "$OUT_DIR" ${USE_MARKET:+--market "$MARKET_DIR"} ;;
      publish) publish_bundle ;;
      *) echo "unknown JOB '$JOB'" ;;
    esac
    ec=$?
    # predict's own exit code stays `job=`; the publish result is its own line so a Mongo
    # hiccup never triggers a re-run of the model (watch_jobs.sh reads both).
    if [ "${JOB:-}" = "predict" ] && [ $ec -eq 0 ] && [ "${PUBLISH:-1}" = "1" ]; then
      publish_bundle; pec=$?
      echo "publish=$pec ($([ $pec -eq 0 ] && echo 'MongoDB updated — deployed console is current' \
        || echo 'FAILED — deployed console still shows the previous run; run: scripts/launch_predict.sh publish'))"
    elif [ "${JOB:-}" = "publish" ]; then
      echo "publish=$ec ($([ $ec -eq 0 ] && echo 'MongoDB updated' || echo 'FAILED'))"
    fi
  else
    echo "FATAL: bundle unpack failed — proceeding to terminate"
    ec=97
  fi
fi
echo "job=$ec ($([ $ec -eq 124 ] && echo 'WATCHDOG TIMEOUT' || echo 'exited')) at $(date -u +%FT%TZ)"
sync 2>/dev/null

[ "${KEEP_POD:-}" = "1" ] && { echo "KEEP_POD=1 — not terminating"; sleep infinity; }
for attempt in $(seq 1 12); do
  timeout 60 python - <<'PY'
import os, ssl, sys, urllib.error, urllib.request as u
pid = os.environ.get("RUNPOD_POD_ID", ""); key = os.environ.get("RUNPOD_TERMINATE_KEY", "")
if not pid or not key: sys.exit(2)
url = "https://rest.runpod.io/v1/pods/" + pid
for ctx in (None, ssl._create_unverified_context()):
    try:
        req = u.Request(url, method="DELETE"); req.add_header("Authorization", "Bearer " + key)
        st = u.urlopen(req, timeout=30, context=ctx).status
        if st == 204: print("terminated (204)"); sys.exit(0)
    except urllib.error.HTTPError as e:
        if e.code == 404: print("already gone (404)"); sys.exit(0)
    except Exception as e:
        print("terminate error:", e, file=sys.stderr)
sys.exit(1)
PY
  [ $? -eq 0 ] && exit 0
  echo "terminate attempt $attempt not confirmed — retry in 20s"; sleep 20
done
echo "!! TERMINATION NOT CONFIRMED — run data_acquisition/scripts/killpod.sh"
sleep 30
