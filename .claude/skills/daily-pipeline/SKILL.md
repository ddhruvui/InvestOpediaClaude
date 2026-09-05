---
name: daily-pipeline
description: Run and monitor the nightly RunPod pipeline end to end — vendor downloads (EODHD, Sharadar, Tiingo, borrow, calendar, FinBERT), then post/validate/build_m1, market, predict, and the reports bundle. Use this whenever the user asks to run the daily pipeline, "launch all", "run launch all and monitor", refresh suggestions, rebuild m1, or check on running pods; and also whenever a pipeline run needs babysitting, a pod looks stuck, a stage failed and needs resuming, or the user asks whether the downloads finished. Prefer this over improvising with launch.sh directly, because the ordering constraints and the "looks finished but silently half-built" failure modes are not visible from the scripts themselves.
---

# Daily pipeline: run and monitor

The pipeline runs entirely on RunPod pods against one network volume. Your job is to launch
it, watch it, **verify each stage actually did its work**, and only then let the next stage
run. The recurring danger is not loud failure — it is a stage that exits, self-terminates,
and leaves stale output that the next stage happily consumes.

Bundled helpers (all read `data_acquisition/runpod/.env`). They live in
`.claude/skills/daily-pipeline/scripts/` — NOT the repo-root `scripts/` — so always call them
by that full path (`SK=.claude/skills/daily-pipeline/scripts` and `$SK/pods` works well):

| script | what it does |
|---|---|
| `pods` | current pods, one per line; empty means none running |
| `vol` | `aws s3` against the volume — `vol ls data/…`, `vol cp data/_run.json /tmp/x --quiet` (note: `_pod_logs/` sits at the volume ROOT, not under `data/`) |
| `podlog <pattern> [n]` | newest matching `_pod_logs/` entry, tailed; works after the pod is gone |
| `verify_fetch.py [FLOOR_ISO]` | per-vendor manifest check; exit 0 only if all fresh and zero hard failures |
| `watch_pods.py` | change-only watchdog: `UP` / `DONE` / `STALL` / `IDLE` |
| `mirror_reports.sh` | OPTIONAL local mirror: pull results off the volume, enforce G-02, rebuild `reports/latest`, (re)publish to MongoDB — the predict pod already publishes on its own |

## Before launching

Check these — each has burned a real run:

1. **Time.** Launch after 21:00 UTC. EODHD publishes the bulk day-file around 23:30 UTC.
   The fetcher re-pulls the trailing few sessions every night (EODHD mutates recent day-files),
   so a same-night pull is not frozen forever — but it IS what the book prices against, so
   launching late still matters. Expect the same-night file ~12% lighter than its neighbors
   (~44k rows vs ~50k): the gap is late-publishing fund-NAV series (`0P…` codes), zero equity
   names, and the next night's re-pull tops it up. In the fetch log, `+N day-files … 1 deferred`
   is normal: the deferred one is the not-yet-published next session (or the 1999-12-31
   backfill boundary), and the deep-history backfill is complete (`~1999-12-31+ remaining`).
2. **No pods already running.** `$SK/pods`. A vendor whose pod is up is skipped, not
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

While pods run, check progress with `$SK/podlog`. Rough shape of a warm run: calendar and
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

`finbert` reporting `added=0` is normal, not a shirked job: it only verifies the pinned
model files (size+sha) are on the volume, so a warm run adds nothing.

**Vendor restatements look like pipeline bugs but aren't.** If build_m1's row counts move by
thousands day-over-day while validate stays green (e.g. `raw_prices_eod` SHRINKING despite a
new session), suspect EODHD restating a single name's history. Diagnose it in one step: diff
the `OK   eod <TICKER>.US: N` lines between the two nights' `fetch.py` pod logs — the ticker
whose count jumped is your answer. Seen 2026-08-28: DD lost its entire pre-2017 (pre-DowDuPont)
tape, −4,444 rows, every other name +0/+1. Daily predict (panel tail 2021+) doesn't care;
note it for the next stage1–3 rerun instead.

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
killer. It has struck twice (once leaving 3 of 8 m1 tables rewritten, once — 2026-08-26's run —
killing BOTH stages on a 2-vCPU pod) while the pod still terminated normally and the manifest
stayed a day old. `launch.sh` now refuses `post`/`m1`/`validate` below 4 vCPU / 8 GB
(`ALLOW_SMALL_POD=1` overrides — don't, unless you accept a possible half-build). If 8 GB ever
OOMs again, relaunch with `RUNPOD_VCPU=8`; the GPU fallback (A4500 = 62 GB) also moots it.

If post failed, do **not** run market/predict; rerun `launch.sh validate` then `launch.sh m1`
**in that order** (build_m1 consumes validate's quarantine.json; neither has post's launch-time
gate) and re-verify all three conditions above.

Then, in order — `market` builds the panel `predict` consumes:

```sh
scripts/launch_predict.sh market      # then confirm job=0 before continuing
scripts/launch_predict.sh predict     # ends by publishing the console bundle to MongoDB
```

**`predict` publishes the results itself.** After the model writes `suggestions.json`, the
same pod runs the G-02 freshness check against the volume, builds the console bundle
(`tools/build_reports.py --volume /workspace`) and pushes it to MongoDB Atlas
(`tools/publish_mongo.py`). The Vercel API serves Mongo and the Render UI serves the API, so
the deployed console is current the moment the pod log shows it — nothing is downloaded to
this machine. The pod gets `MONGO_URI`/`DB_PASSWORD` from `runpod/.env`.

`launch_predict.sh` does not verify startup, so after each launch confirm a
`<ts>-predict-<job>-<podid>.log` appears in `_pod_logs/` within ~3 min. If it never does, the
pod landed on a broken host: it will bill indefinitely while reporting RUNNING, so DELETE it and
relaunch. Never size these below 4 vCPU — 4 GB OOMs them.

## Finish: confirm the publish

The run is done when the predict pod's log carries **all three** of these:

```sh
.claude/skills/daily-pipeline/scripts/podlog predict-predict 30
```

- `job=0` — the model ran
- `publish: G-02 OK as_of_close=<today's close> newest day-file=<same>`
- `publish=0` right after `verify: reports=8 docs for 'latest', predictions history=N,
  suggestions.as_of_close=<today's close>` — the bundle is in MongoDB

Then the deployed console (Render UI → Vercel API → Mongo) already shows it; its header's
`published` timestamp is the proof. `curl -s <vercel>/api/health` returns the same
`published_utc` / `as_of_close` if you want it without a browser.

If `job=0` but `publish=<non-zero>`, the model is fine and only the push failed (Mongo
unreachable, G-02 stale book, a missing stage report). Do **not** rerun predict. Read the
`publish:` lines, fix the cause, then re-publish from the volume on a small pod:

```sh
scripts/launch_predict.sh publish      # 2-vCPU pod, a few minutes; same three log lines
```

`watch_jobs.sh` prints a loud `PUBLISH FAILED` line for this case but treats the job as
done, so `daily.sh` continues into its local mirror step, which re-publishes from here.

### Optional: mirror locally (git record)

Nothing the pods produced is on this machine — they write to the network volume and to
Mongo. If you also want the bundle in the repo (the daily `reports/latest` commits), run:

```sh
.claude/skills/daily-pipeline/scripts/mirror_reports.sh
```

It downloads the artifacts into `./derived`, re-runs G-02, rebuilds `reports/latest` and
publishes it again (idempotent — Mongo reports `unchanged`). Do **not** run
`scripts/daily.sh` for this: that is the full loop and would redo an hour of work.

What it pulls, and why each matters:

| from the volume | to local | why |
|---|---|---|
| `derived/predict/suggestions.json` | `derived/suggestions.json` | the book; the run exists to produce it |
| `derived/predict/suggestions.md` | `reports/suggestions_latest.md` | human-readable book |
| `m1/sessions.parquet` | `derived/sessions.parquet` | session grid the UI calendar needs |
| `derived/stage{1,2,3}/*_report.json` | `derived/` | dashboard gate table and verdict |
| `derived/stage3/*.parquet` | `derived/` | equity curve and trades; only on `FULL_MIRROR=1` or if absent |

It then runs `tools/build_reports.py --src derived --out reports/latest` and
`tools/publish_mongo.py --bundle reports/latest` (the same two steps the pod ran), using
`MONGO_URI`/`DB_PASSWORD` from `runpod/.env`; `SKIP_PUBLISH=1` skips the publish.

**The check that matters is G-02**: the book's `as_of_close` must equal the newest
`eod_bulk` day-file. The script refuses to publish otherwise, because the failure it guards
is silent — EODHD publishes the bulk file late (~19:30 ET), so a chain that built m1 too
early scores the *prior* close and every downstream number still looks plausible. If it
fires, rerun post, market and predict rather than overriding it.

Use `FULL_MIRROR=1` after a stage3 rerun, so the equity/trades parquets are re-pulled rather
than kept from the previous run.

`reports/latest/*.json` is the whole contract with the UI, so if the pages look stale, check
`published_utc` / `as_of_close` on `/api/health` before suspecting the app. Locally,
`cd app/backend && npm start` serves the same thing from Mongo when `app/backend/.env` has
the credentials.

`stage1`, `stage2` and `stage3` are **not** part of the daily loop — they are the research and
backtest stages, rerun only on code/config change or the monthly cadence. The dashboard's
verdict and trade counts come from their existing reports, so seeing unchanged numbers there
is expected, not a bug. When they ARE due, use the **`monthly-pipeline`** skill — it carries
the stage ordering, sizing, runtimes, and the FULL_MIRROR finish.

## Reporting back

Give the user the evidence, not reassurance: a per-vendor table of jobs/ok/fail, the exit code
of every stage, whether today's day-file landed, the G-02 result, the final book size, and the
predict pod's `publish=0` + `verify:` line (what the deployed console now shows). If
something failed, say which stage, what the log showed, and what you did about it.
