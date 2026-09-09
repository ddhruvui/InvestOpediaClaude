#!/usr/bin/env bash
# HOST-SIDE POD REAPER. Kills pods whose job has FINISHED, from this machine, so a run
# never depends on a pod being able to kill itself.
#
# Why this exists: on 2026-09-09 every pod stopped being able to delete itself. The cause
# turned out to be a User-Agent block (Cloudflare 1010 on `Python-urllib/*`, fixed in the
# bootstraps), but the failure mode it exposed is permanent: when self-termination breaks
# for ANY reason, nothing else notices, and the pod:
#   1. bills forever, and
#   2. RESTARTS — RunPod relaunches dockerStartCmd when it exits, so the fetcher RE-RUNS
#      every ~5 min, re-spending vendor API credits (calendar/finbert each ran 5x on
#      2026-09-09 before they were killed by hand), and
#   3. blocks the NEXT night's run: launch.sh SKIPS a vendor whose pod is already up, so
#      that vendor silently never refreshes.
# A pod cannot be trusted to clean up after itself, so this does it from the host instead.
#
# This is NOT killpod.sh. killpod.sh deletes every investopediaclaude-* pod, including
# ones still working, so it is unsafe mid-run. This reaper deletes a pod only after it
# has read that pod's OWN log off the volume and seen the job finish:
#
#   fetchers (eodhd nasdaq tiingo borrow calendar finbert)
#       reaped on `fetch=<rc> (…) — terminating pod <id>`, whatever <rc> is: the work is
#       over either way, the log is already on the volume, and these jobs are resumable.
#       A non-zero rc is reported loudly.
#   post / m1 / validate / predict-* / sync   (GATED)
#       reaped only when the exit code is present AND ZERO. These write the M1 tables and
#       the book; a failed one is left alive and reported so it can be looked at, and
#       --reap-failed (or REAP_FAILED=1) reaps those too. Note the trade-off: a stray
#       failed pod also BLOCKS its own relaunch, because launch_predict.sh skips a job
#       whose pod name is already running — so prefer --reap-failed in automation.
#   KEEP_POD=1 pods are never reaped, ever (that flag means "I am inspecting this").
#   A pod with no log on this volume is never reaped (it may be a JOB=exp pod on the
#   experiment volume — point this script at that one with RUNPOD_VOLUME_ID_OVERRIDE).
#
# Usage:
#   scripts/reap_pods.sh                     # one pass: report every pod, reap the finished ones
#   scripts/reap_pods.sh --watch             # keep polling (default: until --deadline, 9h)
#   scripts/reap_pods.sh --watch --until-empty   # …and stop once no pods are left
#   scripts/reap_pods.sh --dry-run           # report only, delete nothing
#   scripts/reap_pods.sh --reap-failed       # also reap gated jobs that exited non-zero
#   scripts/reap_pods.sh eodhd post          # restrict to these pods (name fragment or id)
#   scripts/reap_pods.sh --force 3tkh6tp97rh5x4  # delete these ids without reading a log
#   RUNPOD_VOLUME_ID_OVERRIDE=<vol-id> scripts/reap_pods.sh  # pods on another volume (JOB=exp)
#
# Env: REAP_POLL (120s), REAP_DEADLINE (32400s), REAP_TAIL_BYTES (8192), REAP_FAILED.
# Every deletion is appended to runpod/reaped-pods.log.
. "$(dirname "$0")/_common.sh"
: "${RUNPOD_API_KEY:?account rpa_ key, set in runpod/.env}"

POLL="${REAP_POLL:-120}"
DEADLINE="${REAP_DEADLINE:-32400}"        # 9h: the pod watchdog is 8h, plus slack
TAIL_BYTES="${REAP_TAIL_BYTES:-8192}"
WATCH=""; UNTIL_EMPTY=""; DRY=""; FORCE=""; ONLY=""
REAP_FAILED="${REAP_FAILED:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --watch)        WATCH=1 ;;
    --until-empty)  UNTIL_EMPTY=1 ;;
    --poll)         POLL="$2"; shift ;;
    --deadline)     DEADLINE="$2"; shift ;;
    --dry-run|-n)   DRY=1 ;;
    --reap-failed)  REAP_FAILED=1 ;;
    --force)        FORCE=1 ;;
    # print the header block: line 2 through the last line that is still a comment
    -h|--help)      sed -n '2,/^[^#]/p' "$0" | sed '$d' | sed 's/^#\{1,\} \{0,1\}//'; exit 0 ;;
    -*)             echo "unknown flag '$1' (see --help)" >&2; exit 2 ;;
    *)              ONLY="$ONLY $1" ;;
  esac
  shift
done
# --force skips the "has this job finished?" check, so it must never be able to sweep
# everything the way killpod.sh does: it only accepts pods named explicitly.
if [ -n "$FORCE" ] && [ -z "$ONLY" ]; then
  echo "--force needs explicit pod ids/names (it skips the finished-job check)" >&2
  echo "  to sweep everything unconditionally that is what scripts/killpod.sh is for" >&2
  exit 2
fi

TMP="$(mktemp -d -t reappods)"
trap 'rm -rf "$TMP"' EXIT
say() { echo "[$(date -u +%H:%M:%SZ)] reap: $*"; }

# name<TAB>id for every investopediaclaude-* pod. Returns non-zero on an unreadable API
# response so a network blip is a "try again later", never "nothing is running".
list_pods() {
  local J
  J="$(curl -sS --max-time 30 https://rest.runpod.io/v1/pods \
        -H "Authorization: Bearer $RUNPOD_API_KEY" 2>/dev/null)" || return 1
  printf '%s' "$J" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
pods = d if isinstance(d, list) else d.get("pods", d.get("data", []))
for p in pods:
    name = str(p.get("name") or "")
    if name.startswith("investopediaclaude-") and p.get("id"):
        print("%s\t%s" % (name, p["id"]))
' || return 1
}

LOGKEYS=""
refresh_logkeys() {
  LOGKEYS="$(aws s3 ls $S3FLAGS "$BUCKET/_pod_logs/" 2>/dev/null | awk '{print $4}')" || LOGKEYS=""
}

# Last TAIL_BYTES of a log. The terminate block is ~2 KB, so this stays tiny even for the
# 260 KB EODHD log we would otherwise re-download on every poll. Falls back to a full copy
# if the volume's S3 gateway ever stops honouring Range.
tail_log() {
  aws s3api get-object $S3FLAGS --bucket "$RUNPOD_VOLUME_ID" --key "_pod_logs/$1" \
      --range "bytes=-${TAIL_BYTES}" "$TMP/tail" >/dev/null 2>&1 \
    || aws s3 cp $S3FLAGS "$BUCKET/_pod_logs/$1" "$TMP/tail" --quiet >/dev/null 2>&1 \
    || return 1
  cat "$TMP/tail"
}

delete_pod() {   # $1 name  $2 id  $3 why
  if [ -n "$DRY" ]; then say "DRY-RUN would reap $1 ($2) — $3"; return 0; fi
  local CODE
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -X DELETE \
    "https://rest.runpod.io/v1/pods/$2" -H "Authorization: Bearer $RUNPOD_API_KEY" 2>/dev/null)" \
    || CODE="000"
  case "$CODE" in
    204|404)
      say "REAPED $1 ($2) — $3 [HTTP $CODE]"
      printf '%s\t%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$2" "$3" \
        >> "$ROOT/runpod/reaped-pods.log" 2>/dev/null || true ;;
    *)
      say "!! FAILED to reap $1 ($2): HTTP $CODE — will retry next pass" ;;
  esac
}

# Reads one pod's newest log and decides what to do with it.
#   VERDICT=reap|hold|wait   REASON=<human text>
classify() {
  local name="$1" id="$2" keys nkeys newest k t code restarts="" found=""
  VERDICT="wait"; REASON="no log on this volume yet"

  keys="$(printf '%s\n' "$LOGKEYS" | grep -- "-${id}\.log$" | sort || true)"
  [ -n "$keys" ] || return 0
  nkeys="$(printf '%s\n' "$keys" | grep -c . || true)"
  newest="$(printf '%s\n' "$keys" | tail -1)"
  # >1 log for one pod id means the container already died and RunPod relaunched it. With
  # the restart guards in place that costs only the pod's time, but before them it re-ran
  # the whole fetcher every ~5 min (calendar and finbert each ran 5x on 2026-09-09).
  [ "$nkeys" -gt 1 ] && restarts=" [RESTARTED ${nkeys}x]"

  t="$(tail_log "$newest")" || { REASON="log unreadable ($newest)"; return 0; }
  if printf '%s' "$t" | grep -q 'KEEP_POD=1'; then
    VERDICT="hold"; REASON="KEEP_POD=1 — deliberately kept alive, never reaped"; return 0
  fi

  # Which line ends this kind of job, and does a non-zero code block the reap?
  local pat gated=""
  case "$name" in
    investopediaclaude-predict-*)                              pat='job='; gated=1 ;;
    investopediaclaude-sync)                                   pat='sync done ec='; gated=1 ;;
    investopediaclaude-post|investopediaclaude-m1|investopediaclaude-validate)
                                                               pat='fetch='; gated=1 ;;
    *)                                                         pat='fetch=' ;;
  esac

  # Take the code from the OLDEST log that carries one — that is the run that did the work.
  # After a restart the newest log is only the guard re-reporting, and the touch-only
  # markers in the predict/sync bootstraps report the sentinel 98 rather than the real
  # outcome; reading the newest log would strand a SUCCESSFUL gated job on hold forever.
  for k in $keys; do
    if [ "$k" = "$newest" ]; then t="$t"; else t="$(tail_log "$k")" || continue; fi
    code="$(printf '%s\n' "$t" | sed -nE "s/.*${pat}(-?[0-9]+).*/\1/p" | tail -1)"
    [ -n "$code" ] && { found="$k"; break; }
  done
  if [ -z "$found" ]; then
    VERDICT="wait"; REASON="still running (${newest})${restarts}"
    return 0
  fi

  if [ "$code" = "0" ]; then
    VERDICT="reap"; REASON="exit=0${restarts}"
  elif [ -z "$gated" ]; then
    VERDICT="reap"; REASON="exit=${code} (FAILED — see ${found})${restarts}"
  elif [ -n "$REAP_FAILED" ]; then
    VERDICT="reap"; REASON="exit=${code} FAILED, reaped by --reap-failed (see ${found})${restarts}"
  else
    VERDICT="hold"
    REASON="exit=${code} FAILED — NOT reaped, read ${found} then: scripts/reap_pods.sh --force ${id}${restarts}"
  fi
}

# post exiting 0 is not proof it BUILT anything: a 4 GB pod has had both its stages
# SIGKILLed while the pod still exited cleanly, leaving a day-stale m1 manifest that
# models then read as current. Reaping is still right (the job is over) — but say so.
warn_if_stale_m1() {
  local MDATE TODAY
  TODAY="$(date -u +%Y-%m-%d)"
  MDATE="$(aws s3 ls $S3FLAGS "$BUCKET/m1/_manifest.json" 2>/dev/null | awk '{print $1}')" || MDATE=""
  [ "$MDATE" = "$TODAY" ] && return 0
  say "!! post exited 0 but m1/_manifest.json is dated '${MDATE:-missing}', not $TODAY —"
  say "   the M1 tables may be half-built; re-run: data_acquisition/scripts/launch.sh post"
}

matches_only() {   # $1 name  $2 id
  [ -z "$ONLY" ] && return 0
  local w
  for w in $ONLY; do
    case "$1" in *"$w"*) return 0 ;; esac
    [ "$2" = "$w" ] && return 0
  done
  return 1
}

# --force: delete exactly what was named, no log read. Used to clear a pod this script
# is deliberately holding, or one whose log never made it to the volume.
if [ -n "$FORCE" ]; then
  PODS="$(list_pods)" || { say "pods API unreadable"; exit 1; }
  n=0
  while IFS=$'\t' read -r NAME ID; do
    [ -n "${ID:-}" ] || continue
    matches_only "$NAME" "$ID" || continue
    delete_pod "$NAME" "$ID" "--force"; n=$((n + 1))
  done <<< "$PODS"
  [ "$n" = "0" ] && say "nothing matched:$ONLY"
  exit 0
fi

STARTED="$(date +%s)"
while true; do
  PODS="$(list_pods)" || PODS="__ERR__"
  if [ "$PODS" = "__ERR__" ]; then
    say "WARN: pods API unreachable — reaping nothing this pass"
  else
    refresh_logkeys
    LIVE=0
    if [ -n "$PODS" ]; then
      while IFS=$'\t' read -r NAME ID; do
        [ -n "${ID:-}" ] || continue
        LIVE=$((LIVE + 1))
        matches_only "$NAME" "$ID" || continue
        classify "$NAME" "$ID"
        case "$VERDICT" in
          reap)
            case "$NAME" in investopediaclaude-post) warn_if_stale_m1 ;; esac
            delete_pod "$NAME" "$ID" "$REASON" ;;
          hold) say "HOLD  ${NAME#investopediaclaude-} ($ID) — $REASON" ;;
          *)    say "wait  ${NAME#investopediaclaude-} ($ID) — $REASON" ;;
        esac
      done <<< "$PODS"
    fi
    [ "$LIVE" = "0" ] && say "no investopediaclaude pods running"
    if [ -n "$WATCH" ] && [ -n "$UNTIL_EMPTY" ] && [ "$LIVE" = "0" ]; then
      say "nothing left to watch — done"; exit 0
    fi
  fi

  [ -n "$WATCH" ] || exit 0
  if [ $(( $(date +%s) - STARTED )) -ge "$DEADLINE" ]; then
    say "deadline (${DEADLINE}s) reached — stopping; re-run to keep watching"; exit 0
  fi
  sleep "$POLL"
done
