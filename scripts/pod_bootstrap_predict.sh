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
# TERMINATE. On 2026-09-09 every DELETE from inside a pod started coming back 403; the
# cause is NOT the pod and NOT the key. Cloudflare fronts rest.runpod.io and now answers
# "error code: 1010" (banned browser signature) to the DEFAULT Python User-Agent, and
# urllib sends `Python-urllib/3.11`. Proof, all from one machine, same key, same URL:
# urllib default UA -> 403, urllib with any other UA -> 404, curl -> 404, curl FORCED to
# `Python-urllib/3.11` -> 403. The laptop "it works from here" test used curl, which is
# why it looked like an inside-the-pod restriction. Hence UA below — do not remove it.
# The ladder then tries GraphQL podTerminate (different host, same UA fix) and runpodctl,
# and NAMES whichever worked, so a future block shows up in the log instead of a mystery.
# None of this is load-bearing: data_acquisition/scripts/reap_pods.sh deletes this pod
# from the host if every rung fails.
for attempt in $(seq 1 6); do
  timeout 90 python - <<'PY'
import json, os, shutil, ssl, subprocess, sys, urllib.error, urllib.request as u

pid = os.environ.get("RUNPOD_POD_ID", "")
key = os.environ.get("RUNPOD_TERMINATE_KEY", "")
if not pid or not key:
    print("MISSING RUNPOD_POD_ID or RUNPOD_TERMINATE_KEY", file=sys.stderr)
    sys.exit(2)
UA = "investopediaclaude-pod/1.0"                  # anything but Python-urllib/*; see above
CTXS = (None, ssl._create_unverified_context())    # verified TLS first, unverified fallback


def _req(url, **kw):
    r = u.Request(url, **kw)
    r.add_header("Authorization", "Bearer " + key)
    r.add_header("User-Agent", UA)
    return r


def rest():
    err = "no attempt"
    for ctx in CTXS:
        try:
            st = u.urlopen(_req("https://rest.runpod.io/v1/pods/" + pid, method="DELETE"),
                           timeout=30, context=ctx).status
            if st == 204:
                return True, "HTTP 204"
            err = "HTTP %s" % st
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True, "HTTP 404 (already gone)"
            err = "HTTP %s%s" % (e.code, " (Cloudflare 1010 — User-Agent blocked)"
                                 if e.code == 403 else "")
        except Exception as e:
            err = str(e)
    return False, err


def graphql():
    body = json.dumps({
        "query": "mutation($id: String!) { podTerminate(input: {podId: $id}) }",
        "variables": {"id": pid},
    }).encode()
    err = "no attempt"
    for ctx in CTXS:
        try:
            req = _req("https://api.runpod.io/graphql", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            payload = json.loads(u.urlopen(req, timeout=30, context=ctx).read().decode() or "{}")
            errs = payload.get("errors") or []
            if not errs:
                return True, "podTerminate accepted"
            code = (errs[0].get("extensions") or {}).get("code")
            if code == "POD_NOT_FOUND":
                return True, "podTerminate (already gone)"
            err = str(errs[0].get("message") or code)
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
        except Exception as e:
            err = str(e)
    return False, err


def runpodctl():
    exe = shutil.which("runpodctl")
    if not exe:
        return False, "not installed on this image"
    try:
        p = subprocess.run([exe, "remove", "pod", pid], timeout=60,
                           env=dict(os.environ, RUNPOD_API_KEY=key),
                           capture_output=True, text=True)
    except Exception as e:
        return False, str(e)
    out = ((p.stdout or "") + " " + (p.stderr or "")).strip().replace("\n", " ")[:160]
    return (p.returncode == 0), ("rc=%s %s" % (p.returncode, out))


for name, fn in (("rest", rest), ("graphql", graphql), ("runpodctl", runpodctl)):
    ok, msg = fn()
    if ok:
        print("TERMINATED via %s: %s" % (name, msg))
        sys.exit(0)
    print("terminate via %s failed: %s" % (name, msg), file=sys.stderr)
sys.exit(1)
PY
  [ $? -eq 0 ] && exit 0
  echo "terminate attempt $attempt did not confirm — retrying in 20s"
  sleep 20
done

# Every rung refused. Exiting hands the pod back to RunPod, which relaunches it — the
# restart guard above keeps that from re-running the job, and the host-side reaper
# (data_acquisition/scripts/reap_pods.sh, run automatically by scripts/daily.sh) deletes
# the pod within a poll or two.
echo "!! TERMINATION NOT CONFIRMED after retries — leaving this pod to the host-side reaper"
echo "   (data_acquisition/scripts/reap_pods.sh)"
sleep 30
