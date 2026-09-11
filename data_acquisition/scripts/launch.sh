#!/usr/bin/env bash
# One launcher, multiple vendors. Pick which fetcher(s) the pod(s) run:
#   scripts/launch.sh            # EODHD  (default) -> src/fetch.py        + config/tickers.json
#   scripts/launch.sh eodhd      # same as above
#   scripts/launch.sh nasdaq     # Sharadar/Nasdaq Data Link -> src/fetch_nasdaq.py + config/sharadar.json
#   scripts/launch.sh tiingo     # Tiingo (tertiary cross-check) -> src/fetch_tiingo.py + config/tiingo.json
#   scripts/launch.sh borrow     # D-10 IBKR borrow fees -> src/fetch_borrow.py + config/borrow.json
#   scripts/launch.sh calendar   # D-11 NYSE sessions (source of truth) -> src/fetch_calendar.py
#   scripts/launch.sh finbert    # D-16 FinBERT weights at a pinned sha -> src/fetch_finbert.py
#   scripts/launch.sh post       # WAITS for today's vendor manifests, then validate -> build_m1.
#                                # Fire it alongside `all`; it self-sequences. This is the stage
#                                # that keeps the M1 tables in step with the data.
#   scripts/launch.sh m1         # §3/§4 landing layer -> src/build_m1.py (Parquet + qlib bridge)
#                                # Needs pandas+pyarrow, installed via PIP_PACKAGES. Run AFTER
#                                # validate, since it consumes quarantine.json.
#   scripts/launch.sh validate   # D-12/M1-04 cross-vendor check + Q-004 + repair -> src/validate.py
#                                # NOT part of `all`: it must run AFTER the eodhd pass, and `all`
#                                # launches pods in parallel. Reads the volume only — no API calls,
#                                # no credits. Idempotent, so re-running is always safe.
#   scripts/launch.sh all        # the DAILY ROUTINE: eodhd + nasdaq + tiingo + borrow + calendar.
#                                # Tiingo's skip_fresh_days makes its warm runs near-free (fresh files
#                                # skipped, budget overruns defer), so daily inclusion costs ~nothing.
#                                # borrow needs no key and finishes in seconds, but it is the one job
#                                # whose MISSED DAYS ARE UNRECOVERABLE (spec G-05) — never drop it.
#
# For each requested vendor it (1) uploads that fetcher + its config to the network volume (code/),
# then (2) creates a CPU pod (pinned to the volume's datacenter) that runs the fetcher and
# self-terminates. The vendors share the volume without colliding (distinct data namespaces).
# Fire-and-forget: prints the pod id(s) and exits. download/clear/storage_usage/killpod are shared.
#
# DRY_RUN=1 prints what would be uploaded/launched without touching S3 or creating pods.
. "$(dirname "$0")/_common.sh"

case "${1:-eodhd}" in
  all)            VENDORS="eodhd nasdaq tiingo borrow calendar finbert" ;;
  eodhd)          VENDORS="eodhd" ;;
  nasdaq|sharadar) VENDORS="nasdaq" ;;
  tiingo)         VENDORS="tiingo" ;;
  borrow|ibkr)    VENDORS="borrow" ;;
  calendar)       VENDORS="calendar" ;;
  finbert)        VENDORS="finbert" ;;
  m1|landing)     VENDORS="m1" ;;
  post)           VENDORS="post" ;;
  validate|qa)    VENDORS="validate" ;;
  *)
    echo "unknown vendor '$1' (valid: eodhd, nasdaq, tiingo, borrow, calendar, finbert, validate, m1, post, all)" >&2; exit 2 ;;
esac
: "${RUNPOD_API_KEY:?account rpa_ key, set in runpod/.env}"

DC="${RUNPOD_DATACENTER:-EU-RO-1}"
IMAGE="${RUNPOD_IMAGE:-python:3.11-slim}"
STORE_LOGS="${STORE_LOGS:-false}"     # when true, the fetcher stores a run log on success (errors always log)
DRY_RUN="${DRY_RUN:-}"

# CPU flavors RunPod may rent (cpuFlavorPriority defaults to "availability", so it
# rents whichever listed flavor is free). Valid: cpu3c cpu3g cpu3m cpu5c cpu5g cpu5m.
FLAVORS="${RUNPOD_CPU_FLAVORS:-[\"cpu3c\",\"cpu3g\",\"cpu3m\",\"cpu5c\",\"cpu5g\",\"cpu5m\"]}"

# Container disk is host-local scratch, NOT the network volume: every fetcher writes its
# data to /workspace (the volume), so this only has to hold the image + pip deps (~1-2 GB).
# It is also a REAL placement constraint — on 2026-08-27 EU-RO-1 refused a 20 GB request at
# every vCPU count and flavor while an otherwise identical 10 GB request placed instantly.
# Keep this small; raise it per-run with RUNPOD_CONTAINER_DISK_GB if a job ever needs it.
CONTAINER_DISK="${RUNPOD_CONTAINER_DISK_GB:-10}"

# GPU FALLBACK. EU-RO-1 runs out of CPU capacity for hours at a time (2026-08-24, -26, -27),
# and the volume pins us to that one datacenter, so "wait for CPU" can mean missing the open.
# GPU hosts are a separate pool and are frequently free when every CPU flavor is refused, so
# every CPU job falls back to the CHEAPEST placeable GPU. This is a capacity workaround, not
# a compute change: the jobs are pure-Python/pandas and never touch CUDA.
#
# The economics favour it. Measured in EU-RO-1 on 2026-08-27, 1 GPU with the volume attached:
#   RTX A4500      $0.25/hr  12 vCPU  62 GB RAM
#   RTX 4000 Ada   $0.28/hr   9 vCPU  50 GB RAM
#   RTX 4090       $0.74/hr  16 vCPU  61 GB RAM
# A GPU pod carries FAR more RAM than any CPU flavor (4-16 GB), so the fallback also makes the
# 8 GB memory floor moot. Jobs run minutes, so the delta is cents. Cheapest first; A4500 is
# ~5x the RAM of the 4-vCPU CPU pod for a few cents an hour more.
# Set GPU_FALLBACK=0 to disable, or RUNPOD_GPU_FALLBACK_TYPES to reorder.
GPU_FALLBACK="${GPU_FALLBACK:-1}"
GPU_FALLBACK_TYPES="${RUNPOD_GPU_FALLBACK_TYPES:-NVIDIA RTX A4500|NVIDIA RTX 4000 Ada Generation|NVIDIA GeForce RTX 3090|NVIDIA RTX A6000|NVIDIA GeForce RTX 4090|NVIDIA A40}"

FAILED=""

# One pod per vendor at a time: a vendor whose pod is still running (e.g. tiingo's ~5h paced run)
# is skipped, not doubled — makes a daily `all` idempotent. Fail-open: if the list call errors,
# RUNNING_PODS is empty and launches proceed.
RUNNING_PODS=$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
  -H "Authorization: Bearer ${RUNPOD_API_KEY}" 2>/dev/null) || RUNNING_PODS=""

launch_vendor() {
  local VENDOR="$1" FETCH_SCRIPT CONFIG_FILE TOKEN_VAR TOKEN_VAL DATA_SUBDIR VCPU
  # RunPod gives 2 GB per vCPU. Default 2 vCPU / 4 GB is fine for the streaming fetchers; nasdaq
  # merges every ticker's history in memory during a cold pull and OOM-killed (exit 137) at 4 GB
  # once the window went to 26 years, so it gets 4 vCPU / 8 GB. m1 loads Parquet frames and gets
  # the same. Override globally with RUNPOD_VCPU.
  VCPU="${RUNPOD_VCPU:-2}"
  case "$VENDOR" in
    # nasdaq no longer needs 8 GB: _stream_bulk_zip made the whole-market pull constant-memory
    # (~99 MB peak) after the buffered version SIGKILLed a 4 GB pod on full-history SF1. The stale
    # 4-vCPU request became pure cost — on 2026-08-18 it failed 24 capacity retries over an hour in
    # EU-RO-1 while 2 vCPU placed immediately and completed 1,513 jobs with 0 failures, including
    # both bulk zips (SF1 681,727 rows, ACTIONS 668,409). m1/post still ask for 4: they hold whole
    # tables in pandas, which streaming does not help.
    nasdaq)              VCPU="${RUNPOD_VCPU:-2}" ;;
    m1|post|validate)    VCPU="${RUNPOD_VCPU:-4}" ;;
  esac
  # MEMORY FLOOR. RunPod gives 2 GB per vCPU, and on 2026-08-27 a 2-vCPU (4 GB) post pod had
  # BOTH stages SIGKILLed (exit -9) partway through: validate died before writing quarantine.json
  # and build_m1 died after 6 of 8 tables. The pod still terminated normally, so m1/_manifest.json
  # silently stayed a day stale with a half-rewritten table set — models then read yesterday's
  # tables believing them current. Refuse rather than half-build; ALLOW_SMALL_POD=1 to override
  # (e.g. deliberately taking the only slot available to hold post.py's manifest gate).
  case "$VENDOR" in
    m1|post|validate)
      if [ "$VCPU" -lt 4 ] && [ -z "${ALLOW_SMALL_POD:-}" ]; then
        echo "REFUSING $VENDOR at ${VCPU} vCPU ($((VCPU * 2)) GB): needs >= 4 vCPU / 8 GB." >&2
        echo "  4 GB SIGKILLs these stages partway and leaves a STALE m1 manifest behind." >&2
        echo "  Set ALLOW_SMALL_POD=1 to override if you accept a possible half-build." >&2
        return 1
      fi ;;
  esac
  case "$VENDOR" in
    eodhd)
      FETCH_SCRIPT="fetch.py";        CONFIG_FILE="tickers.json";  DATA_SUBDIR="data"
      TOKEN_VAR="EODHD_API_TOKEN";    TOKEN_VAL="${EODHD_API_TOKEN:-}" ;;
    nasdaq)
      FETCH_SCRIPT="fetch_nasdaq.py"; CONFIG_FILE="sharadar.json"; DATA_SUBDIR="data_nasdaq"
      TOKEN_VAR="SHARADAR_API_KEY";   TOKEN_VAL="${SHARADAR_API_KEY:-${NASDAQ_DATA_LINK_API_KEY:-}}" ;;
    tiingo)
      FETCH_SCRIPT="fetch_tiingo.py"; CONFIG_FILE="tiingo.json";   DATA_SUBDIR="data_tiingo"
      TOKEN_VAR="TIINGO_API_TOKEN";   TOKEN_VAL="${TIINGO_API_TOKEN:-}" ;;
    borrow)
      # No vendor key: the IBKR short-stock file is anonymous FTP. TOKEN_VAR is passed through as a
      # harmless empty env var so the payload shape stays identical across vendors.
      FETCH_SCRIPT="fetch_borrow.py"; CONFIG_FILE="borrow.json";   DATA_SUBDIR="data_borrow"
      TOKEN_VAR="IBKR_FTP_USER";      TOKEN_VAL="${IBKR_FTP_USER:-shortstock}" ;;
    calendar)
      # The only fetcher with a pip dep: exchange_calendars (D-11 needs FUTURE sessions).
      FETCH_SCRIPT="fetch_calendar.py"; CONFIG_FILE="calendar.json"; DATA_SUBDIR="data_calendar"
      # PINNED: spec D-11 says "pin package version; refresh on upgrade" — exchange_calendars
      # ships holiday-rule corrections in point releases, so an unpinned install can
      # silently change the session list (and therefore Q-001) between two runs.
      TOKEN_VAR="PIP_PACKAGES";       TOKEN_VAL="${PIP_PACKAGES:-exchange_calendars==4.13.2}" ;;
    finbert)
      # One-time weights pull, but idempotent (size+sha checked), so it is safe in the daily set.
      FETCH_SCRIPT="fetch_finbert.py"; CONFIG_FILE="finbert.json"; DATA_SUBDIR="data_finbert"
      TOKEN_VAR="HF_ENDPOINT";        TOKEN_VAL="${HF_ENDPOINT:-https://huggingface.co}" ;;
    post)
      # Waits for the fetchers, then runs validate + build_m1 in one pod. Same pip deps as m1,
      # and the same 8 GB — it ends up doing the M1 build itself.
      FETCH_SCRIPT="post.py";         CONFIG_FILE="calendar.json"; DATA_SUBDIR="m1"
      TOKEN_VAR="PIP_PACKAGES";       TOKEN_VAL="${PIP_PACKAGES:-pandas pyarrow}" ;;
    m1)
      # The landing layer is the one job with heavy pip deps; it reads every vendor tree off the
      # volume and writes the M1 Parquet tables + qlib CSVs back to it.
      FETCH_SCRIPT="build_m1.py";     CONFIG_FILE="calendar.json"; DATA_SUBDIR="m1"
      TOKEN_VAR="PIP_PACKAGES";       TOKEN_VAL="${PIP_PACKAGES:-pandas pyarrow}" ;;
    validate)
      # Consumes the other vendors' output; no config file of its own and no credential.
      FETCH_SCRIPT="validate.py";     CONFIG_FILE="calendar.json"; DATA_SUBDIR="data_quality"
      TOKEN_VAR="VALIDATE_ARGS";      TOKEN_VAL="${VALIDATE_ARGS:---repair}" ;;
  esac
  if [ -z "$TOKEN_VAL" ]; then
    echo "SKIP $VENDOR: $TOKEN_VAR not set in runpod/.env" >&2
    return 1
  fi
  if printf '%s' "$RUNNING_PODS" | grep -q "investopediaclaude-${VENDOR}"; then
    echo "SKIP $VENDOR: pod investopediaclaude-${VENDOR} is already running (its run is in progress)"
    return 0
  fi

  if [ -n "$DRY_RUN" ]; then
    echo "DRY_RUN: would upload src/$FETCH_SCRIPT + src/bootstrap.sh + config/$CONFIG_FILE to $BUCKET/code/"
    echo "DRY_RUN: would create CPU pod investopediaclaude-${VENDOR} in ${DC} (FETCH_SCRIPT=$FETCH_SCRIPT, token=$TOKEN_VAR)"
    return 0
  fi

  echo "Uploading $VENDOR code to $BUCKET/code/ ..."
  aws s3 cp $S3FLAGS "$ROOT/src/$FETCH_SCRIPT"      "$BUCKET/code/$FETCH_SCRIPT"
  if [ "$VENDOR" = "post" ]; then      # post shells out to these two
    aws s3 cp $S3FLAGS "$ROOT/src/validate.py"  "$BUCKET/code/validate.py"
    aws s3 cp $S3FLAGS "$ROOT/src/build_m1.py"  "$BUCKET/code/build_m1.py"
  fi
  aws s3 cp $S3FLAGS "$ROOT/src/bootstrap.sh"       "$BUCKET/code/bootstrap.sh"
  aws s3 cp $S3FLAGS "$ROOT/config/$CONFIG_FILE"    "$BUCKET/code/$CONFIG_FILE"

  # post.py's manifest gate: "fresh" = ended_at newer than this launch (see post.py docstring)
  local PAYLOAD RESP CODE BODY POD_ID LAUNCHED_AT
  LAUNCHED_AT=$(date -u +%Y-%m-%dT%H:%M:%S+00:00)
  # COMPUTE is the only part that differs between a CPU pod and the GPU fallback.
  build_payload() {   # $1: "" for CPU, else a gpuTypeId
    local COMPUTE
    if [ -z "$1" ]; then
      COMPUTE="\"computeType\": \"CPU\", \"vcpuCount\": ${VCPU}, \"cpuFlavorIds\": ${FLAVORS}"
    else
      COMPUTE="\"computeType\": \"GPU\", \"gpuCount\": 1, \"gpuTypeIds\": [\"$1\"]"
    fi
    cat <<JSON
{
  "name": "investopediaclaude-${VENDOR}",
  ${COMPUTE},
  "cloudType": "SECURE",
  "imageName": "${IMAGE}",
  "networkVolumeId": "${RUNPOD_VOLUME_ID}",
  "containerDiskInGb": ${CONTAINER_DISK},
  "volumeMountPath": "/workspace",
  "dataCenterIds": ["${DC}"],
  "dockerStartCmd": ["bash", "/workspace/code/bootstrap.sh"],
  "env": {
    "${TOKEN_VAR}": "${TOKEN_VAL}",
    "TIINGO_API_TOKEN2": "${TIINGO_API_TOKEN2:-}",
    "TIINGO_API_TOKEN3": "${TIINGO_API_TOKEN3:-}",
    "FETCH_SCRIPT": "${FETCH_SCRIPT}",
    "CONFIG_PATH": "/workspace/code/${CONFIG_FILE}",
    "DATA_DIR": "/workspace/${DATA_SUBDIR}",
    "RUNPOD_TERMINATE_KEY": "${RUNPOD_API_KEY}",
    "STORE_LOGS": "${STORE_LOGS}",
    "POST_LAUNCHED_AT": "${LAUNCHED_AT}"
  }
}
JSON
  }

  # Try CPU first (cheapest); on a capacity refusal walk the GPU list cheapest-first.
  local KIND
  attempt_create() {   # $1: "" for CPU, else gpuTypeId
    PAYLOAD=$(build_payload "$1")
    RESP=$(curl -sS -w $'\n%{http_code}' -X POST https://rest.runpod.io/v1/pods \
      -H "Authorization: Bearer ${RUNPOD_API_KEY}" \
      -H 'Content-Type: application/json' \
      -d "$PAYLOAD")
    CODE=$(printf '%s' "$RESP" | tail -n1)
    BODY=$(printf '%s' "$RESP" | sed '$d')
    [ "$CODE" = "200" ] || [ "$CODE" = "201" ]
  }

  echo "Creating $VENDOR CPU pod in ${DC} ..."
  KIND="CPU ${VCPU}vCPU"
  if ! attempt_create ""; then
    # Only a capacity shortage justifies paying for a GPU; a malformed request or a bad
    # token must still fail loudly rather than silently retrying six more times.
    if [ "$GPU_FALLBACK" = "1" ] && printf '%s' "$BODY" | grep -q "no longer any instances\|no instances currently available"; then
      echo "  no CPU capacity in ${DC} — falling back to the cheapest available GPU" >&2
      local OLDIFS="$IFS" G
      IFS='|'
      for G in $GPU_FALLBACK_TYPES; do
        IFS="$OLDIFS"
        [ -n "$G" ] || continue
        echo "  trying GPU: $G" >&2
        if attempt_create "$G"; then KIND="GPU $G"; break; fi
        IFS='|'
      done
      IFS="$OLDIFS"
    fi
  fi

  if [ "$CODE" != "200" ] && [ "$CODE" != "201" ]; then
    echo "$VENDOR pod create failed (HTTP $CODE):" >&2
    echo "$BODY" >&2
    echo "Hint: if it complains about CPU flavor, set RUNPOD_CPU_FLAVORS in runpod/.env" >&2
    echo "(valid flavors: cpu3c cpu3g cpu3m cpu5c cpu5g cpu5m)" >&2
    [ "$GPU_FALLBACK" = "1" ] && echo "GPU fallback also found nothing free in ${DC}." >&2
    return 1
  fi
  echo "  placed on: $KIND"

  if command -v jq >/dev/null; then
    POD_ID=$(printf '%s' "$BODY" | jq -r '.id // empty')
  else
    # NOT the old greedy sed: `.*"id"` matches the LAST "id" in the body, so a nested
    # networkVolume.id wins and you get the volume id instead of the pod id (see
    # launch_predict.sh, which shipped that bug and mis-reported a pod on 2026-08-27).
    POD_ID=$(printf '%s' "$BODY" | \
      python3 -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null)
  fi

  if [ -n "${POD_ID:-}" ]; then
    printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$VENDOR" "$POD_ID" >> "$ROOT/runpod/launched-pods.log"
  fi
  echo "Launched $VENDOR pod ${POD_ID:-?} — it will fetch data to ${BUCKET}/ and self-terminate."

  # STARTUP VERIFICATION. `desiredStatus: RUNNING` is NOT proof the job started: RunPod can place a
  # pod on a bad machine where the container never launches, and it then sits allocated and BILLING
  # with no public IP and no output. Observed repeatedly, once for 70 minutes. The only reliable
  # signal is bootstrap.sh's own log appearing on the volume, so poll for that and relaunch if it
  # never shows — the replacement usually lands on a different machine.
  [ -n "${POD_ID:-}" ] || return 0
  local i=0
  while [ $i -lt "${STARTUP_CHECKS:-12}" ]; do
    sleep "${STARTUP_POLL_SEC:-15}"
    i=$((i + 1))
    if aws s3 ls $S3FLAGS "$BUCKET/_pod_logs/" 2>/dev/null | grep -q -- "-${POD_ID}.log"; then
      echo "  $VENDOR pod $POD_ID confirmed started (bootstrap log on volume)"
      return 0
    fi
  done
  echo "  !! $VENDOR pod $POD_ID produced no bootstrap log in $((i * ${STARTUP_POLL_SEC:-15}))s" >&2
  echo "     (dead RunPod machine — killing it so it stops billing, and retrying once)" >&2
  curl -sS -X DELETE "https://rest.runpod.io/v1/pods/$POD_ID" \
    -H "Authorization: Bearer ${RUNPOD_API_KEY}" -o /dev/null 2>/dev/null || true
  return 42   # caller relaunches
}

# _common.sh sets -e; each leg runs under `if` so one vendor's failure still launches the rest,
# and the script exits nonzero listing what failed.
for V in $VENDORS; do
  # `set -e` (from _common.sh) would abort the script on a non-zero return before rc could be
  # read, so capture it in a condition context.
  rc=0; launch_vendor "$V" || rc=$?
  if [ "$rc" = "42" ]; then          # accepted but never started — one retry
    # `set -e` (from _common.sh) would abort the script on a non-zero return before rc could be
  # read, so capture it in a condition context.
  rc=0; launch_vendor "$V" || rc=$?
    [ "$rc" = "42" ] && rc=1
  fi
  [ "$rc" != "0" ] && FAILED="$FAILED $V"
done

if [ -n "$FAILED" ]; then
  echo "FAILED to launch:$FAILED" >&2
  exit 1
fi
[ -n "$DRY_RUN" ] && exit 0
echo "Check data later with: scripts/download.sh   (if a pod outlives its job: scripts/reap_pods.sh)"
