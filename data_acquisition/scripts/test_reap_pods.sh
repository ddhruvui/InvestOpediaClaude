#!/usr/bin/env bash
# Unit test for reap_pods.sh's decision logic — the part that decides whether a pod is
# finished and whether it is safe to delete. It extracts the REAL classify() out of
# reap_pods.sh and drives it against fabricated pod logs, so no pod, volume or API is
# touched. Run it after any change to reap_pods.sh:  scripts/test_reap_pods.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
STUB="$(mktemp -d -t reaptest)"
trap 'rm -rf "$STUB"' EXIT

# classify() reads logs through tail_log; point that at files we write instead.
tail_log() { [ -f "$STUB/$1" ] || return 1; cat "$STUB/$1"; }
eval "$(awk '/^classify\(\) \{/,/^\}$/' "$HERE/reap_pods.sh")"

PASS=0; FAIL=0
mklog() { printf '%s\n' "$2" > "$STUB/$1"; }          # $1 key, $2 body
t() {  # $1 desc  $2 want  $3 name  $4 id  $5 keys(newline-sep)  [$6 REAP_FAILED]
  local desc="$1" want="$2"
  LOGKEYS="$5"; REAP_FAILED="${6:-}"; VERDICT=""; REASON=""
  classify "$3" "$4"
  if [ "$VERDICT" = "$want" ]; then
    PASS=$((PASS + 1)); printf '  ok   %-50s -> %-4s | %s\n' "$desc" "$VERDICT" "$REASON"
  else
    FAIL=$((FAIL + 1)); printf '  FAIL %-50s -> got %s, want %s | %s\n' "$desc" "$VERDICT" "$want" "$REASON"
  fi
}

DONE0='fetch=0 (exited) at 2026-09-09T00:37:37Z — terminating pod aaa
terminate HTTPError: 403
!! TERMINATION NOT CONFIRMED after retries'
DONE1='fetch=1 (exited) at 2026-09-09T00:37:37Z — terminating pod aaa'
MID='OK   eod    SRE.US: 6710 (+6710) [full] -> /workspace/data/SRE.json'
K1=20260909T003730Z-fetch_calendar.py-aaa.log
K2=20260909T004218Z-fetch_calendar.py-aaa.log

echo "--- fetchers: reaped on any exit code (work is over, logs are on the volume) ---"
mklog "$K1" "$DONE0"; t "fetcher exit 0"                    reap investopediaclaude-calendar aaa "$K1"
mklog "$K1" "$DONE1"; t "fetcher exit 1 — reaped but flagged" reap investopediaclaude-calendar aaa "$K1"
mklog "$K1" 'fetch=124 (WATCHDOG TIMEOUT) at X — terminating pod aaa'
                      t "fetcher watchdog timeout 124"      reap investopediaclaude-eodhd    aaa "$K1"
mklog "$K1" 'fetch=-9 (exited) at X — terminating pod aaa'
                      t "fetcher SIGKILL -9"                reap investopediaclaude-nasdaq   aaa "$K1"
mklog "$K1" "$MID";   t "fetcher mid-run"                   wait investopediaclaude-eodhd    aaa "$K1"

echo "--- gated jobs: the exit code must be present AND zero ---"
mklog "$K1" "$DONE0"; t "post exit 0"                       reap investopediaclaude-post     aaa "$K1"
mklog "$K1" "$DONE1"; t "post exit 1 -> HOLD"               hold investopediaclaude-post     aaa "$K1"
mklog "$K1" "$DONE1"; t "post exit 1 + --reap-failed"       reap investopediaclaude-post     aaa "$K1" 1
mklog "$K1" "$DONE1"; t "m1 exit 1 -> HOLD"                 hold investopediaclaude-m1       aaa "$K1"
mklog "$K1" "$DONE1"; t "validate exit 1 -> HOLD"           hold investopediaclaude-validate aaa "$K1"
mklog "$K1" 'post: waiting for fresh vendor manifests'
                      t "post still waiting on manifests"   wait investopediaclaude-post     aaa "$K1"

echo "--- predict / sync ---"
mklog "$K1" $'job=0 (exited) at X\npublish=0 (MongoDB updated)'
                      t "predict job=0, published"          reap investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" $'job=0 (exited) at X\npublish=6 (FAILED)'
                      t "predict job=0 but publish failed"  reap investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" 'job=1 (exited) at X'
                      t "predict job=1 -> HOLD"             hold investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" 'job=124 (WATCHDOG TIMEOUT) at X'
                      t "predict watchdog -> HOLD"          hold investopediaclaude-predict-market  aaa "$K1"
mklog "$K1" 'predict bootstrap 2026-09-09 pod=aaa job=predict'
                      t "header 'job=predict' is not a code" wait investopediaclaude-predict-predict aaa "$K1"
mklog "$K1" 'sync done ec=0 at X'
                      t "sync ec=0"                         reap investopediaclaude-sync     aaa "$K1"
mklog "$K1" 'sync done ec=1 at X'
                      t "sync ec=1 -> HOLD"                 hold investopediaclaude-sync     aaa "$K1"

echo "--- restarts: the OLDEST log holds what the job actually did ---"
mklog "$K1" "$DONE0"
mklog "$K2" 'RESTART DETECTED — the fetcher already ran (exit 0); not re-running
fetch=0 (exited) at X — terminating pod aaa'
                      t "restarted fetcher, original exit 0" reap investopediaclaude-calendar aaa "$K1
$K2"
mklog "$K1" $'job=0 (exited) at X\npublish=0'
mklog "$K2" 'RESTART DETECTED (marker exists) — skipping job, terminating
job=98 (exited) at X'
                      t "restarted predict: 98 must not mask job=0" reap investopediaclaude-predict-predict aaa "$K1
$K2"
mklog "$K1" "$MID"; mklog "$K2" "$MID"
                      t "restarted mid-fetch, no code yet"  wait investopediaclaude-eodhd    aaa "$K1
$K2"

echo "--- never reaped ---"
mklog "$K1" $'job=0 (exited) at X\nKEEP_POD=1 — not terminating'
                      t "KEEP_POD=1 even with job=0"        hold investopediaclaude-predict-stage1 aaa "$K1"
                      t "no log here (exp pod, other volume)" wait investopediaclaude-predict-exp aaa '20260909T00Z-predict-exp-OTHER.log'
rm -f "$STUB/$K1";    t "log unreadable"                    wait investopediaclaude-eodhd    aaa "$K1"

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" = "0" ]
