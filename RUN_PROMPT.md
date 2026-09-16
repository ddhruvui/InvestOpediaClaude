# Run prompt — nightly pipeline

Paste one of these into Claude Code from the repo root. The `daily-pipeline` skill
(`.claude/skills/daily-pipeline/`) carries the ordering rules and verification steps, so the
prompt itself can stay short — it does not need to re-explain the pipeline.

## The one to use

> Run launch all and keep monitoring. Make sure the downloads from every service complete,
> verify them, and then run the remaining steps. While the pods run, watch for errors and for
> pods stuck in a bad state. Do the needful.

Best fired **after 21:00 UTC** (17:00 ET). EODHD publishes the bulk day-file around 23:30 UTC,
so later in the evening is safer, not worse. (The fetcher re-pulls the trailing few sessions
each night, so an early or light pull self-heals the next night — but the book prices against
tonight's pull, so launch late anyway. The same-night file always runs ~12% light: late fund-NAV
series, no equity names.)

## Variants

**Data already fetched, just redo the modelling:**

> The vendor fetch is already done for tonight. Verify the manifests are fresh, then run
> market and predict and rebuild the reports bundle.

**Resume after a failure:**

> The pipeline failed partway last night. Work out which stage broke, fix it, and carry the
> run through to the reports bundle. Tell me what actually failed and why.

**Just check on it:**

> Check the pipeline: what pods are running, how far along are they, and is anything stuck?

**Force a full model refit** (normally automatic every 21 labelled sessions):

> Run the daily pipeline, but force a from-scratch refit rather than a warm update.

## What "done" looks like

Ask for these back, and treat anything missing as not-done:

- Per-vendor `jobs / ok / fail` — **fail must be 0** across all six trees
- The session that just closed present in `data/eod_bulk/US/`
- `validate exit=0` **and** `build_m1 exit=0`, with an `m1/_manifest.json` from this run
- `market job=0`, `predict job=0`
- **G-02**: `suggestions.json`'s `as_of_close` equals the newest day-file
- The book size, from the predict log's `suggestions written:` line
- **Published to MongoDB by the predict pod** — its log ends with `publish=0` after
  `verify: ... suggestions.as_of_close=<today's close>`; the deployed console shows the
  previous run until this happens (`scripts/launch_predict.sh publish` re-publishes)

Nothing lands on this machine: the pods write to the network volume, and the predict pod
publishes the console bundle to MongoDB itself. If only that last step failed, say so rather
than re-running the pipeline:

> Predict already ran on the pod. Just re-publish the bundle to MongoDB.

(That is `scripts/launch_predict.sh publish` — a 2-vCPU pod, under a minute.) Then view it on
the deployed console (Render UI → Vercel API → MongoDB). The header shows the `published`
timestamp, so a stale page is obvious.

A pod exiting is not evidence a stage succeeded — a stage can be OOM-killed (`exit=-9`) while
its pod still self-terminates normally and leaves yesterday's output in place. Ask for exit
codes, not "it finished".

## Minute bars (`intraday-pull` skill)

The 1-minute intraday store (`data/tickdata/` on the volume; every stock we hold — the S&P list, the
data-only watchlist, the research names — plus SPY/QQQ, ~519 symbols, extended hours, from 2004) has
its own skill, `.claude/skills/intraday-pull/`. It is append-only (existing bars are never
rewritten) and the pod grows the volume 1 GB at a time when free space drops under 5 GB. The nightly
`launch all` tops it up; use the prompt below for a first backfill, after a universe change, or
whenever the intraday pod reported `fetch=75` (space) or `fetch=1` (failures).

**The one to use:**

> Run the intraday pull: launch the minute-bar fetcher on RunPod, monitor it, grow the volume by
> 1 GB if it runs out of space, verify the downloaded data, and make sure the pod is gone.

**Just check on it:**

> How far is the minute-bar backfill? Verify the intraday store and tell me what is missing.

**Done means:** `fetch=0`, the pod gone, the runner's append-only check with 0 lost rows and 0 shrunk
files, and the verifier printing `VERIFIED: minute store consistent — 519 symbols …`. While a new
batch of names is still backfilling (a cold symbol is ~350 credits, so ~400 new names take two
nights of credits), exit 5 `NOT FINISHED (consistent so far)` is the expected answer.

## Not included

`stage1`, `stage2`, `stage3` are the research/backtest stages and are deliberately outside the
daily loop; they are rerun on code or config changes, or on the monthly cadence to refresh the
G-11 gate. The dashboard's verdict, equity curve and trade counts come from their existing
reports, so those numbers not moving after a daily run is expected.

Ask for them explicitly if you want them — the `monthly-pipeline` skill
(`.claude/skills/monthly-pipeline/`) carries that runbook:

> Run the monthly stage refresh.

or, for a partial rerun:

> Also rerun stage1 and stage3 to refresh the gate verdict.
