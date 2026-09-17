---
name: daily-pipeline
description: Run and monitor the nightly MODEL pipeline on RunPod — gate on a current volume, then market (m1x whole-market panel), predict (continual warm update -> target book -> suggestions), and the MongoDB publish of the console bundle. Use this whenever the user asks to run the daily pipeline, refresh suggestions, run market/predict, check on running pods, or whether the console is current; and whenever a run needs babysitting, a pod looks stuck, or a stage failed and needs resuming. The vendor downloads (EODHD, Sharadar, Tiingo, borrow, calendar, FinBERT, minute bars) and post/validate/build_m1 are NOT here — they are the separate DataAcquistion repo (../DataAcquistion, its daily-fetch skill); this skill starts by checking that repo's output landed on the volume. Prefer this over improvising with launch_predict.sh directly, because the ordering, the freshness gate and the "looks finished but silently stale" failure modes are not visible from the scripts themselves.
---

# Daily pipeline: run and monitor (models)

This repo owns the MODEL half of the day. The DATA half — every vendor pull and the
`validate` → `build_m1` landing layer — lives in the **DataAcquistion** repo
(`/Users/dhruvdesai/Development/DataAcquistion`, its own `scripts/daily.sh` and
`daily-fetch` skill). The two repos share nothing but the RunPod network volume: that repo
writes the raw vendor trees and `m1/` at the volume root; this one reads them and writes ONLY
under `results/InvestOpediaClaude/` (`m1x/`, `derived/`, `models/`, `ledger/`, `reports/`, its
own `_pod_logs/` and code bundle), then publishes to MongoDB from there. **Never launch a
fetcher from here, and never write to the volume root.** If the data is not current, the fix
is to run the fetch in that repo, not to improvise one. `results/ResearchGate/` is a separate
project on the same volume — leave it alone.

Your job is to check the volume is current, launch the model stages, watch them, **verify
each stage actually did its work**, and confirm the publish. The recurring danger is not
loud failure — it is a stage that exits, self-terminates, and leaves stale output behind.

Bundled helpers (all read `runpod/.env`). They live in `.claude/skills/daily-pipeline/scripts/`
— NOT the repo-root `scripts/` — so always call them by that full path
(`SK=.claude/skills/daily-pipeline/scripts` and `$SK/pods` works well):

| script | what it does |
|---|---|
| `pods` | current pods, one per line; empty means none running |
| `vol` | `aws s3` against the volume — `vol ls m1/`, `vol cp results/InvestOpediaClaude/derived/predict/suggestions.json /tmp/x --quiet` (this repo's pod logs are `results/InvestOpediaClaude/_pod_logs/`; the root `_pod_logs/` is DataAcquistion's) |
| `podlog <pattern> [n]` | newest matching pod log from either log dir, tailed; works after the pod is gone |
| `watch_pods.py` | change-only watchdog: `UP` / `DONE` / `STALL` / `IDLE` |

Reaping is a repo-root script, not one of these: `scripts/reap_pods.sh` (a copy of the
DataAcquistion one; both repos reap on the same account) deletes a pod once that pod's OWN
log shows its job finished. `scripts/daily.sh` runs it in the background for the whole run,
so a hand-driven launch is the case that needs it — start `reap_pods.sh --watch` alongside
the watchdog, or run it bare for a one-pass status.

## The one-command form

```sh
scripts/daily.sh
```

does everything below (gate → market → predict → publish check) and ends with
`DONE — MongoDB updated`. The sections that follow are for driving or debugging it by hand.

## Before launching: the gate

The models must not run on stale tables. Check, in this order:

1. **Is the volume current?** Two objects tell you, both via `$SK/vol ls`:
   - `m1/_manifest.json` — written by the DataAcquistion post stage. It must be **newer than
     the newest `data/eod_bulk/US/<DATE>.json` day-file** (post consumed the latest pull) and
     no more than ~36 h old.
   - the newest day-file itself must be **the session that just closed** — that is what the
     book will price against (G-02 later checks `as_of_close` equals it).
   `scripts/daily.sh` applies exactly this gate and refuses otherwise (`SKIP_DATA_CHECK=1`
   overrides). If it fails, the answer is `(cd ../DataAcquistion && scripts/daily.sh)` — or,
   if only post failed there, its `launch.sh validate` then `launch.sh m1`. Do not run market or
   predict until the manifest is fresh.
2. **No pods already running.** `$SK/pods`. A job whose pod is up is skipped, not doubled.
   A pod from the DataAcquistion fetch may legitimately still be up (`investopediaclaude-intraday`
   runs for hours) — that one is not a reason to wait; post does not depend on it either.
3. **Sleep.** On macOS, sleeping suspends every background loop you start (a run once lost
   8.5 h this way). Hold the machine awake for the run only, tied to your chain's pid:
   `caffeinate -dims -w <pid> &`.

## Run, in order — `market` builds the panel `predict` consumes

```sh
scripts/launch_predict.sh market      # then confirm job=0 before continuing
scripts/launch_predict.sh predict     # ends by publishing the console bundle to MongoDB
```

`launch_predict.sh` does not verify startup, so after each launch confirm a
`<ts>-predict-<job>-<podid>.log` appears in `results/InvestOpediaClaude/_pod_logs/` within ~3 min. If it never does, the
pod landed on a broken host: it will bill indefinitely while reporting RUNNING, so DELETE it and
relaunch. Never size these below 4 vCPU — 4 GB OOMs them. If EU-RO-1 has no CPU, the launcher
falls back automatically to the cheapest available GPU — expect `placed on: GPU NVIDIA RTX
A4500`; that is normal and correct, the job still runs the CPU image.

`scripts/watch_jobs.sh market` / `predict` watches a job to completion with ONE auto-relaunch
(it reaps a failed pod first, because a dead-but-present pod blocks its own relaunch).

## Monitor

Start the watchdog and leave it attached to a Monitor — it only speaks on state changes:

```sh
POLL=180 STALL_CHECKS=5 python3 .claude/skills/daily-pipeline/scripts/watch_pods.py
```

While pods run, check progress with `$SK/podlog predict-market` / `predict-predict`. A
`STALL` line means the pod is alive but its log has not grown — read the log before acting.

A pod that stays up AFTER its log ends in `job=<rc>` has failed to delete itself. RunPod
relaunches the container; the restart guard stops the job re-running, but the pod bills and
blocks the next launch as "already running". Reap it — `scripts/reap_pods.sh` — rather than
waiting it out. A SUCCESSFUL predict pod that got restarted writes a second log whose
`job=98` looks like a failure: read the OLDEST log for that pod id.

**`predict` publishes the results itself.** After the model writes `suggestions.json`, the
same pod runs the G-02 freshness check against the volume, builds the console bundle
(`tools/build_reports.py --volume /workspace`, reading `results/InvestOpediaClaude/derived/`,
writing `results/InvestOpediaClaude/reports/latest/`) and pushes it to MongoDB Atlas
(`tools/publish_mongo.py`). The Vercel API serves Mongo and the Render UI serves the API, so
the deployed console is current the moment the pod log shows it — nothing is downloaded to
this machine. The pod gets `MONGO_URI`/`DB_PASSWORD` from `runpod/.env`.

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
done; `daily.sh` then fails at its final check with the same re-publish command. A G-02
failure (`as_of_close` ≠ newest day-file) means the book priced against an older tape than
the volume now holds — that is the gate above having been skipped or the fetch having landed
mid-run; re-run market → predict.

Nothing is kept on this machine: the pods write to the network volume and to MongoDB, and
`reports/` is not in the repo. If the pages look stale, check `published_utc` / `as_of_close`
on `/api/health` before suspecting the app. Locally, `cd app/backend && npm start` serves the
same Mongo data when `app/backend/.env` has the credentials.

**Vendor restatements look like model bugs but aren't.** If the m1 row counts moved by
thousands day-over-day, EODHD restated a single name's history (seen 2026-08-28: DD lost its
pre-2017 tape; restored 2026-09-15). The DataAcquistion repo's fetch logs are where that is
diagnosed; daily predict (panel tail 2021+) doesn't care, the stage1–3 rerun does.

`stage1`, `stage2` and `stage3` are **not** part of the daily loop — they are the research and
backtest stages, rerun only on code/config change or the monthly cadence. The dashboard's
verdict and trade counts come from their existing reports, so seeing unchanged numbers there
is expected, not a bug. When they ARE due, use the **`monthly-pipeline`** skill — it carries
the stage ordering, sizing, runtimes, and the publish-from-volume finish.

## Reporting back

Give the user the evidence, not reassurance: the gate result (m1 manifest timestamp vs the
newest day-file), the exit code of every stage, the G-02 result, the final book size, and the
predict pod's `publish=0` + `verify:` line (what the deployed console now shows). If
something failed, say which stage, what the log showed, and what you did about it.
