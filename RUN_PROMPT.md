# Run prompt — nightly models

Paste one of these into Claude Code from the repo root. The `daily-pipeline` skill
(`.claude/skills/daily-pipeline/`) carries the ordering rules, the freshness gate and the
verification steps, so the prompt itself can stay short.

**The vendor downloads are not run from here.** The `DataAcquistion` repo
(`../DataAcquistion`) fills the RunPod network volume — its `scripts/daily.sh` / `daily-fetch`
skill / `RUN_PROMPT.md` — and this repo reads what it left behind. Run that first (it ends with
`DONE — the volume is current`), then this.

## The one to use

> Check the volume is current — m1 rebuilt after the newest day-file, and that day-file is the
> session that just closed — then run market and predict, watch for errors and for pods stuck
> in a bad state, and confirm the bundle was published to MongoDB. Do the needful.

## Variants

**Resume after a failure:**

> The model pipeline failed partway last night. Work out which stage broke, fix it, and carry
> the run through to the MongoDB publish. Tell me what actually failed and why.

**Just check on it:**

> Check the pipeline: what pods are running, how far along are they, and is anything stuck?

**Force a full model refit** (normally automatic every 21 labelled sessions):

> Run the daily model pipeline, but force a from-scratch refit rather than a warm update.

**Re-publish only:**

> Predict already ran on the pod. Just re-publish the bundle to MongoDB.

(That is `scripts/launch_predict.sh publish` — a 2-vCPU pod, under a minute.)

## What "done" looks like

Ask for these back, and treat anything missing as not-done:

- The gate: `m1/_manifest.json` newer than the newest `data/eod_bulk/US/` day-file, and that
  day-file is the session that just closed (if not, the fix is in the DataAcquistion repo)
- `market job=0`, `predict job=0`
- **G-02**: `suggestions.json`'s `as_of_close` equals the newest day-file
- The book size, from the predict log's `suggestions written:` line
- **Published to MongoDB by the predict pod** — its log ends with `publish=0` after
  `verify: ... suggestions.as_of_close=<today's close>`; the deployed console shows the
  previous run until this happens (`scripts/launch_predict.sh publish` re-publishes)

Nothing lands on this machine: the pods write to the network volume, and the predict pod
publishes the console bundle to MongoDB itself. View it on the deployed console (Render UI →
Vercel API → MongoDB); the header shows the `published` timestamp, so a stale page is obvious.

A pod exiting is not evidence a stage succeeded — a stage can be OOM-killed (`exit=-9`) while
its pod still self-terminates normally and leaves yesterday's output in place. Ask for exit
codes, not "it finished".

## Not included

`stage1`, `stage2`, `stage3` are the research/backtest stages and are deliberately outside the
daily loop; they are rerun on code or config changes, or on the monthly cadence to refresh the
G-11 gate. The dashboard's verdict, equity curve and trade counts come from their existing
reports, so those numbers not moving after a daily run is expected. The `monthly-pipeline`
skill (`.claude/skills/monthly-pipeline/`) carries that runbook:

> Run the monthly stage refresh.

The vendor fetch, post/validate/build_m1 and the minute-bar store are the `DataAcquistion`
repo's job; ask for them there.
