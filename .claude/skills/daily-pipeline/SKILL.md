---
name: daily-pipeline
description: Run and monitor the nightly RunPod pipeline end to end — vendor downloads (EODHD, Sharadar, Tiingo, borrow, calendar, FinBERT), then post/validate/build_m1, market, predict, and the reports bundle. Use this whenever the user asks to run the daily pipeline, "launch all", "run launch all and monitor", refresh suggestions, rebuild m1, or check on running pods; and also whenever a pipeline run needs babysitting, a pod looks stuck, a stage failed and needs resuming, or the user asks whether the downloads finished. Prefer this over improvising with launch.sh directly, because the ordering constraints and the "looks finished but silently half-built" failure modes are not visible from the scripts themselves.
---

# Daily pipeline: run and monitor

The pipeline runs entirely on RunPod pods against one network volume. Your job is to launch
it, watch it, **verify each stage actually did its work**, and only then let the next stage
run. The recurring danger is not loud failure — it is a stage that exits, self-terminates,
and leaves stale output that the next stage happily consumes.

Bundled helpers (all read `data_acquisition/runpod/.env`; run them from the repo root):

| script | what it does |
|---|---|
| `scripts/pods` | current pods, one per line; empty means none running |
| `scripts/vol` | `aws s3` against the volume — `vol ls data/…`, `vol cp data/_run.json /tmp/x --quiet` |
| `scripts/podlog <pattern> [n]` | newest matching `_pod_logs/` entry, tailed; works after the pod is gone |
| `scripts/verify_fetch.py [FLOOR_ISO]` | per-vendor manifest check; exit 0 only if all fresh and zero hard failures |
| `scripts/watch_pods.py` | change-only watchdog: `UP` / `DONE` / `STALL` / `IDLE` |
| `scripts/mirror_reports.sh` | pull results off the volume, enforce G-02, rebuild `reports/latest` |

## Before launching

Check these — each has burned a real run:

1. **Time.** Launch after 21:00 UTC. EODHD publishes the bulk day-file around 23:30 UTC, and
   a day-file pulled mid-session is frozen incomplete forever (the fetcher never re-pulls an
   existing one). Running after ~00:00 UTC is fine and often better.
2. **No pods already running.** `scripts/pods`. A vendor whose pod is up is skipped, not
   doubled, so a stray pod silently means that vendor does not refresh.
3. **Baseline the volume** so you can prove movement later: newest `data/eod_bulk/US/*.json`,
   and the `_run.json` timestamp of each vendor tree. Note the newest day-file — the run must
   add the session that just closed.
4. **Sleep.** On macOS, sleeping suspends every background loop you start (a run once lost
   8.5 h this way). Hold the machine awake for the run only, tied to your chain's pid:
   `caffeinate -dims -w <pid> &`.

## Launch

```sh
data_acquisition/scripts/launch.sh all && data_acquisition/scripts/launch.sh post
```

Fire `post` immediately after `all`, never later. `post` waits for vendor manifests **newer
than its own launch** (minus a 10-min grace), so a `post` started after the fetchers finish
waits out its full 240-min timeout and then builds anyway with recorded gaps.

`launch.sh` verifies each pod actually started (its bootstrap log appears on the volume) and
retries a dead placement once. If EU-RO-1 has no CPU, it falls back automatically to the
cheapest available GPU — expect lines like `placed on: GPU NVIDIA RTX A4500`. That is normal
and correct; the job still runs the CPU image.

## Monitor

Start the watchdog and leave it attached to a Monitor — it only speaks on state changes:

```sh
POLL=180 STALL_CHECKS=5 python3 .claude/skills/daily-pipeline/scripts/watch_pods.py
```

While pods run, check progress with `scripts/podlog`. Rough shape of a warm run: calendar and
finbert finish in seconds; nasdaq ~10 min; borrow ~30 min; tiingo ~35 min; **eodhd ~70-80 min**
and is always the long pole; `post` then takes ~7 min once its gate clears.

A `STALL` line means the pod is alive but its log has not grown — read the log before acting;
it may be a slow vendor rather than a hang. `post` is deliberately exempt, because it prints
its gate line once and then polls in silence for as long as the fetchers take.

## Verify the downloads

Do not treat "pod gone" as success. Run:

```sh
python3 .claude/skills/daily-pipeline/scripts/verify_fetch.py <post-launch-ISO-minus-10min>
```

Every tree must be `FRESH` with `fail=0`, and `STALE`/`HARD FAILURES` must both be none.
Then confirm the session that just closed actually landed:

```sh
.claude/skills/daily-pipeline/scripts/vol ls data/eod_bulk/US/ | tail -3
```

Tiingo `DEFER` entries are budget deferrals, not failures — they resume next run.

`borrow` is the one job whose misses are permanent: the IBKR snapshot is a live file with no
history. Confirm its `borrow usa: N rows [snapshot …]` line appears. Its iBorrowDesk half only
refreshes ~80 of 506 names per run, which is by design (each fetch returns a rolling year that
closes the gap), so a large "stale" count there is not an error.

## Verify post, then run the rest

`post` runs validate then build_m1 **inside one pod**, so they are not separate pod logs:

```sh
.claude/skills/daily-pipeline/scripts/podlog post.py 30
```

Require **three** things, not one: `validate exit=0`, `build_m1 exit=0`, and an `m1/_manifest.json`
whose timestamp is from this run. A negative exit code is a signal death — `exit=-9` is the OOM
killer, and it has previously left m1 with 3 of 8 tables rewritten while the pod still terminated
normally and the manifest stayed a day old. If post failed, do **not** run market/predict; rerun
`launch.sh validate` then `launch.sh m1` (neither has post's launch-time gate) and re-verify.

Then, in order — `market` builds the panel `predict` consumes:

```sh
scripts/launch_predict.sh market      # then confirm job=0 before continuing
scripts/launch_predict.sh predict
```

`launch_predict.sh` does not verify startup, so after each launch confirm a
`<ts>-predict-<job>-<podid>.log` appears in `_pod_logs/` within ~3 min. If it never does, the
pod landed on a broken host: it will bill indefinitely while reporting RUNNING, so DELETE it and
relaunch. Never size these below 4 vCPU — 4 GB OOMs them.

## Finish: pull the results down for the UI

Nothing the pods produced is on your machine yet — they write to the network volume. This
step copies the artifacts into `./derived`, checks them, and rebuilds the bundle the app
serves. It is the only thing that needs to run after `predict`:

```sh
.claude/skills/daily-pipeline/scripts/mirror_reports.sh
```

Do **not** run `scripts/daily.sh` for this. That is the full loop — it would re-run fetch,
post, market and predict, redoing an hour of work to accomplish a two-minute copy.

What it pulls, and why each matters:

| from the volume | to local | why |
|---|---|---|
| `derived/predict/suggestions.json` | `derived/suggestions.json` | the book; the run exists to produce it |
| `derived/predict/suggestions.md` | `reports/suggestions_latest.md` | human-readable book |
| `m1/sessions.parquet` | `derived/sessions.parquet` | session grid the UI calendar needs |
| `derived/stage{1,2,3}/*_report.json` | `derived/` | dashboard gate table and verdict |
| `derived/stage3/*.parquet` | `derived/` | equity curve and trades; only on `FULL_MIRROR=1` or if absent |

It then runs `tools/build_reports.py --src derived --out reports/latest`, which is what the
API actually serves — the app recomputes nothing.

**The check that matters is G-02**: the book's `as_of_close` must equal the newest
`eod_bulk` day-file. The script refuses to publish otherwise, because the failure it guards
is silent — EODHD publishes the bulk file late (~19:30 ET), so a chain that built m1 too
early scores the *prior* close and every downstream number still looks plausible. If it
fires, rerun post, market and predict rather than overriding it.

Use `FULL_MIRROR=1` after a stage3 rerun, so the equity/trades parquets are re-pulled rather
than kept from the previous run.

To view it:

```sh
cd app/backend && npm install && npm start     # http://localhost:8787 — API + built UI
```

`reports/latest/*.json` is the whole contract with the UI, so if the pages look stale, check
that bundle's timestamps before suspecting the app.

`stage1`, `stage2` and `stage3` are **not** part of the daily loop — they are the research and
backtest stages, rerun only on code/config change or the monthly cadence. The dashboard's
verdict and trade counts come from their existing reports, so seeing unchanged numbers there
is expected, not a bug.

## Reporting back

Give the user the evidence, not reassurance: a per-vendor table of jobs/ok/fail, the exit code
of every stage, whether today's day-file landed, the G-02 result, and the final book size. If
something failed, say which stage, what the log showed, and what you did about it.
