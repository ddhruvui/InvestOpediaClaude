---
name: monthly-pipeline
description: Run the monthly research/backtest refresh — stage1 (LGBM gates), stage2 (GRU+CNN+FinBERT on GPU), stage3 (meta gate + barrier book + CPCV) — then FULL_MIRROR the results and rebuild the dashboard bundle. Use this whenever the user asks for the monthly run, to rerun stage1/stage2/stage3, to refresh the G-11 gate verdict, the equity curve, or the backtest, or after a code/config change that invalidates the stage reports. NOT for the nightly loop (that is daily-pipeline) and NOT for the model's 21-session full refit (that happens automatically inside the daily predict job).
---

# Monthly pipeline: the stage1–3 refresh

The daily loop deliberately never touches `stage1`/`stage2`/`stage3`. They produce the
research reports the dashboard serves — the G-11 verdict, gate table, member Rank ICs,
CPCV spread, equity curve, and the barrier trade book — and are rerun on the monthly
cadence or whenever code or `configs/system.yaml` changes. Two things this skill is NOT:
the nightly run (use `daily-pipeline`), and the LGBM full refit (automatic in the daily
`predict` job every 21 labeled sessions — it needs no runbook).

Reuse the daily skill's helpers throughout: `SK=.claude/skills/daily-pipeline/scripts`
(`$SK/pods`, `$SK/podlog`, `$SK/vol`).

## Preconditions

1. **Last night's daily run was green** — `validate exit=0`, `build_m1 exit=0`, fresh
   `m1/_manifest.json`, `market job=0`. The stages read m1 and the m1x market panel; running
   them on a stale or half-built m1 bakes bad data into a month of dashboard numbers.
2. **No `investopediaclaude-predict-*` pods running** (`$SK/pods`) — a running pod makes the
   launcher skip that job silently.
3. **Daytime is fine.** Unlike the fetchers there is no publish-time constraint; the stages
   read the volume only. Stage2 is the long pole (~5–6 h on GPU), so start early enough that
   stage3 and the mirror finish the same day.
4. Optional but cheap pre-flight: `scripts/launch_predict.sh test` — the T-01..T-15 suite on
   a throwaway pod, ~2 min. 25 tests must pass.

## Run order

`stage1 → stage2 → stage3`, strictly — stage3 consumes the scores directory the earlier
stages write. All via the repo-root launcher (NOT the daily `launch.sh`):

```sh
scripts/launch_predict.sh stage1     # features -> LGBM heads (purged WF) -> book -> gates; CPU, ~20 min
scripts/launch_predict.sh stage2     # + GRU + JKX CNN + FinBERT; NATIVE GPU pod, ~5-6 h
scripts/launch_predict.sh stage3     # meta gate + barrier-exit book + CPCV(6,2); CPU, ~15 min
```

Facts that matter, all from `launch_predict.sh`:

- CPU jobs default to `RUNPOD_VCPU=8` (16 GB). Never go below 4 vCPU — 4 GB OOMs these jobs.
- **stage2 is the one true GPU job**: PyTorch image, 40 GB container disk, RTX 4090/A5000/A40.
  The cheap-GPU *fallback* (A4500 etc.) applies only to the CPU jobs, never to stage2.
- stage3 reads `SCORES_DIR` (default `/workspace/derived/stage2`) with
  `SCORES_DIR_ALT=/workspace/derived/stage1` as the fallback. To gate on stage1 only —
  skipping stage2 — set `SCORES_DIR=/workspace/derived/stage1` explicitly.
- `KEEP_POD=1` keeps a pod alive for inspection; `NO_CPCV=1` skips the CPCV block if you only
  need a quick stage3 book.

**Startup verification is on you**: `launch_predict.sh` does not check that the pod actually
started. After each launch, confirm a `<ts>-predict-<job>-<podid>.log` appears in `_pod_logs/`
within ~3 min; if it never does, the pod is on a broken host billing forever while RUNNING —
DELETE it and relaunch.

## Monitor

```sh
WATCH_SINCE=$(date -u +%Y%m%dT%H%M%SZ) scripts/watch_jobs.sh stage1
# then, as each finishes:
WATCH_SINCE=... scripts/watch_jobs.sh stage2 stage3
```

`watch_jobs.sh` polls every 10 min, declares success on a `job=0` tail, and auto-relaunches a
failed job ONCE (at `RUNPOD_VCPU=8`). Always pass `WATCH_SINCE` — without it, yesterday's
`job=0` log reads as instant success while today's pod is still booting. Stage2's in-pod
watchdog allows 18 h, so a long quiet stretch is not a hang; read the log before killing
anything. Success check per stage: `$SK/podlog "predict-stage1" 5` ends in `job=0`, and fresh
`*_report.json` files land under `derived/stage1|stage2|stage3/` on the volume.

## Finish: FULL_MIRROR, rebuild, commit

The daily mirror deliberately skips the heavy stage3 parquets when they already exist — after
a stage3 rerun that default would keep serving LAST month's equity curve and trades. So:

```sh
FULL_MIRROR=1 .claude/skills/daily-pipeline/scripts/mirror_reports.sh
```

That re-pulls `derived/stage{1,2,3}/*_report.json` AND the stage3 equity/trades parquets,
then rebuilds `reports/latest` (the whole contract with the UI) and publishes it to MongoDB
for the deployed console. Verify the dashboard bundle
moved: `reports/latest/manifest.json` `built_utc` is from this run and `summary.json`'s
verdict/gate rows changed date. Then commit the refreshed `derived/` + `reports/` files, as
the daily mirror commits do.

## Caveats worth knowing before comparing numbers

- **Vendor restatements accumulate between monthly runs.** EODHD restated DD on 2026-08-28,
  deleting its pre-2017 (pre-DowDuPont) history — the first rerun after that date will shift
  slightly vs the 2026-08-24/25 reports for reasons that are the vendor's, not the code's.
  Check memory / `git log` for restatement notes before attributing drift to a code change.
- A `configs/system.yaml` change does two things: it invalidates the stage reports (why you
  are here) and changes `config_hash`, which forces the NEXT daily `predict` into a full
  refit automatically. Expect that daily run to be slower and its book to shift.
- Every stage fit logs to the G-09 trials ledger (`/workspace/ledger/trials.parquet`) —
  that is by design (the Deflated Sharpe's N must count every attempt); never reset it.
