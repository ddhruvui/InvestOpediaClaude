---
name: intraday-pull
description: Launch, monitor and verify the 1-minute intraday bar pull (extended hours included) for EVERY stock we hold (S&P list + data-only watchlist + research names + SPY/QQQ, ~519 symbols) on RunPod — the fetcher src/fetch_intraday.py writing append-only Parquet to data/tickdata on the network volume, growing the volume 1 GB at a time when space runs low. Use whenever the user asks to pull, refresh, backfill or verify minute bars, tick data or intraday data, to add symbols to the minute store, or about the volume running out of space during the minute pull; also for "is the intraday pod done", "how far is the backfill", or the nightly incremental top-up. Prefer this over calling launch.sh directly, because the credit budget, the append-only proof, the grow-and-relaunch fallback and the verification live here.
---

# Intraday pull: minute bars for every stock we hold

One pod, one fetcher, one folder. `data_acquisition/src/fetch_intraday.py` pulls EODHD **1-minute**
bars (the only interval with history to 2004 and documented pre-market/after-hours) and writes
`data/tickdata/1m/<SYMBOL>/<YYYY>.parquet` on the volume.

**Universe** (resolved at run time, nothing to keep in sync): `config/intraday.json` `stocks` (the
100 research names + TSM/ASML/ARM, pulled first) ∪ the `stocks`/`market` of `tickers.json` and
`watchlist_eodhd.json` ∪ `market` (SPY, QQQ) → **519 symbols** on 2026-09-15. An S&P change or a new
watchlist name is picked up on the next run and backfilled from 2004 (or its listing) automatically.

**Append-only** (user requirement): an existing bar is never replaced; only timestamps the store
lacks are added. A year file with nothing new is not touched; one that gains bars is rebuilt
atomically with its old rows unchanged (Parquet cannot append in place).

Each run, per symbol: **tail** from `last_ts − 4 days` to now (the vendor keeps filling a session for
hours after the close — see memory `eodhd-intraday-publishes-late`); a **cold** symbol first probes
the last 10 days and, if the vendor has nothing (BF-B today), is recorded as *no vendor data* and
rechecked weekly instead of burning 70 requests; then a one-time **gap pass** re-asks every XNYS
session missing inside the symbol's history, once — vendor holes are recorded in `_audit/` and never
re-billed.

Bundled scripts (they read `data_acquisition/runpod/.env`; nothing is typed). `SK=.claude/skills/intraday-pull/scripts`:

| script | what it does |
|---|---|
| `python3 $SK/run_intraday.py` | snapshot (rows + closed-year files) → grow if free < 5 GB → `launch.sh intraday` → progress every minute → `fetch=<rc>` → confirm the pod is gone (reap if not) → on 75 grow 1 GB + relaunch; on a time cap relaunch → append-only check → verify |
| `python3 $SK/verify_intraday.py` | manifest + `_run.json` + the pod's full-history `_verify.json` against the LOCAL universe, plus two Parquet files checked structurally. Exit **0** verified, **5** consistent but not finished, **1** problems, **4** no store |
| `python3 $SK/run_intraday.py --status` | report only, no launch |

## Run it

1. `python3 $SK/run_intraday.py` from the repo root, in the background; check its output about every
   10 minutes. Progress lines are the pod's `[ n/519] SYMBOL +rows in N req …` and a `.. n/519 symbols,
   requests, budget left, free GB, grown GB` heartbeat every 25 symbols.
2. For a manual daytime run after the nightly eodhd pod has finished, the 25k credit reserve is more
   than needed: `INTRADAY_RESERVE_CREDITS=10000 python3 $SK/run_intraday.py`. Keep the default for
   anything that may overlap the nightly eodhd pod.
3. Pod exit codes (`fetch=`): **0** done or capped (`_run.json` `stop_reason`: `budget` → continues
   after 00:00 UTC, `time` → the runner relaunches); **1** symbol failures or verification failed
   (listed); **75** the pod could not grow the volume itself → the runner grows 1 GB and relaunches.

## Space rule (user requirement)

Grow by **1 GB at a time**, only when needed. The pod keeps `min_free_gb` (5 GB — room for the
nightly `m1x` rebuild, 4.5 GB) free: before every request and write it checks, and below the floor
it PATCHes the volume +1 GB through the RunPod API (key and `NETWORK_VOLUME_ID` are in its env),
waits for the new size, re-checks, repeats — at most 30 GB per run. Its log shows
`space: statvfs total … allocated … -> mode …` at start and one `growing volume … N GB -> N+1 GB`
line per step. The host-side `grow_volume.sh 1` / `volume_free.sh` are the fallback. RunPod
volumes only grow.

## Credits and time

5 credits per 120-day request. Cold symbol ≈ 70 requests (350 credits); warm night ≈ 1 request per
symbol (~2.6k credits). The run stops at `cap − reserve_credits` (re-read every 500 requests, so a
concurrent eodhd pod's spend counts) and at `max_run_minutes` 430 (inside the 8 h watchdog). Eight
worker threads: ~2.8 s per request server-side, so ~160 requests a minute.

## What "done" looks like

- `fetch=0`, a `TERMINATED via rest: HTTP 204` line, and the pod gone from the pod list
- runner: `append-only check: … 0 lost rows; closed-year files … untouched … 0 shrank/vanished`
- verifier: `VERIFIED: minute store consistent — 519 symbols (N without vendor intraday)`, meaning
  every symbol caught up, 0 structural-error symbols, 0 historical sessions left un-asked, extended
  hours on ≥ 95% of fresh symbols, every 09:30 bar within 10 bps of the daily open
- during a multi-night backfill, exit 5 `NOT FINISHED (consistent so far)` is the expected answer;
  report symbols with data, requests used, and how many are left

Report those numbers back, not "it finished". A pod exiting is not evidence.

## Nightly

`launch.sh all` includes `intraday`, so the daily pipeline tops the store up on its own (post.py does
not wait for it). A nightly run right after the close sees late-publishing sessions partially;
the 4-day resettle adds the missing minutes the next night — don't re-run for that. Run this skill by
hand for a first backfill, after a universe change, or when the nightly intraday pod shows `fetch=1`
or `fetch=75`.

## The prompt that triggers this skill

> Run the intraday pull: launch the minute-bar fetcher on RunPod, monitor it, grow the volume by
> 1 GB if it runs out of space, verify the downloaded data, and make sure the pod is gone.
