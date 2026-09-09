#!/usr/bin/env bash
# One-shot data-sync pod (branch aggressive-short-horizon): mounts the
# EXPERIMENT volume at /workspace and copies the prediction-stack inputs from
# the SOURCE volume over the S3 API — read-only GETs against the source, which
# is never written. `aws s3 sync` makes re-runs resumable.
# HARD RULE (same as pod_bootstrap_predict.sh): control ALWAYS reaches the
# self-termination block; a restart hits the marker and terminates.
set +e
BOOT_LOG_DIR="/workspace/_pod_logs"
mkdir -p "$BOOT_LOG_DIR" 2>/dev/null
BOOT_LOG="$BOOT_LOG_DIR/$(date -u +%Y%m%dT%H%M%SZ)-sync-${RUNPOD_POD_ID:-nopod}.log"
exec > >(tee -a "$BOOT_LOG") 2>&1
echo "sync bootstrap $(date -u +%FT%TZ) pod=${RUNPOD_POD_ID:-?} src=${SRC_VOLUME_ID:-?}"

MARKER="/workspace/_pod_logs/.ran-${RUNPOD_POD_ID:-nopod}"
ec=98
if [ -f "$MARKER" ]; then
  echo "RESTART DETECTED (marker exists) — skipping sync, terminating"
elif [ -z "${SRC_VOLUME_ID:-}" ] || [ -z "${RUNPOD_S3_ENDPOINT:-}" ]; then
  echo "FATAL: SRC_VOLUME_ID / RUNPOD_S3_ENDPOINT unset"; ec=97
else
  touch "$MARKER" 2>/dev/null
  timeout 600 python -m pip install --quiet --no-input --disable-pip-version-check awscli \
    || echo "!! pip awscli failed"
  SRC="s3://${SRC_VOLUME_ID}"
  EP=(--endpoint-url "$RUNPOD_S3_ENDPOINT" --region "${RUNPOD_S3_REGION:-eu-ro-1}")
  ec=0
  if [ "${SYNC_SET:-inputs}" = "raw" ]; then
    # RAW MODE (adoption rebuild): copy ONLY the downloaded vendor trees +
    # the DSR trials ledger. Everything derived (m1, m1x, scores, models)
    # is rebuilt from scratch on this volume by the pipeline itself.
    for tree in data data_nasdaq data_tiingo data_borrow data_calendar                 data_finbert data_quality ledger; do
      echo ">> $SRC/$tree/ -> /workspace/$tree/"
      timeout 14400 aws s3 sync "${EP[@]}" --only-show-errors \
        "$SRC/$tree" "/workspace/$tree" --exclude 'logs/*' || ec=1
    done
    echo "---- volume contents after raw sync ----"
    du -sh /workspace/* 2>/dev/null
    echo "sync done ec=$ec at $(date -u +%FT%TZ)"
    sync 2>/dev/null
    [ "${KEEP_POD:-}" = "1" ] && { echo "KEEP_POD=1 — not terminating"; sleep infinity; }
  else
  echo ">> $SRC/m1/ -> /workspace/m1/"
  timeout 3600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/m1" /workspace/m1 || ec=1
  echo ">> $SRC/m1x/ -> /workspace/m1x/"
  timeout 14400 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/m1x" /workspace/m1x || ec=1
  echo ">> $SRC/data/market/ -> /workspace/data/market/"
  timeout 3600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/data/market" /workspace/data/market || ec=1
  echo ">> $SRC/derived/stage2/ (scores + sentiment) -> /workspace/derived/stage2/"
  timeout 7200 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/derived/stage2" /workspace/derived/stage2 \
    --exclude '*' --include 'scores_*.parquet' --include 'sentiment_scores.parquet' \
    --include 'stage2_report.json' || ec=1
  echo ">> $SRC/derived/stage1/ (scores) -> /workspace/derived/stage1/"
  timeout 3600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/derived/stage1" /workspace/derived/stage1 \
    --exclude '*' --include 'scores_*.parquet' || ec=1
  echo ">> $SRC/ledger/ -> /workspace/ledger/ (seed DSR trial count)"
  timeout 600 aws s3 sync "${EP[@]}" --only-show-errors "$SRC/ledger" /workspace/ledger || ec=1
  echo "---- volume contents after sync ----"
  du -sh /workspace/* 2>/dev/null
  echo "sync done ec=$ec at $(date -u +%FT%TZ)"
  fi
fi
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
