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
- Results pulled **down to this machine** — `derived/suggestions.json` and
  `reports/suggestions_latest.md` present and dated today
- `reports/latest/` rebuilt (that bundle is the whole contract with the UI), plus the book size

The pods write to the network volume, not to your disk — until the mirror step runs there is
nothing local to look at and the UI still shows the previous run. If you only want that last
step, say so rather than re-running the pipeline:

> Everything already ran on the pods. Just pull the results down and rebuild the reports bundle.

Then view it with `cd app/backend && npm start` (http://localhost:8787).

A pod exiting is not evidence a stage succeeded — a stage can be OOM-killed (`exit=-9`) while
its pod still self-terminates normally and leaves yesterday's output in place. Ask for exit
codes, not "it finished".

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
