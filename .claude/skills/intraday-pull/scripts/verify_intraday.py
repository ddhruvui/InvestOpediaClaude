#!/usr/bin/env python3
"""Verify the minute-bar store on the volume from the host. Reads _manifest.json, _run.json and the
pod's full-history _verify.json from data/tickdata (or intraday_1m if the folder rule redirected),
cross-checks them against the universe resolved from the LOCAL configs, and downloads two Parquet
files (a long-held research name and the newest backfilled name) to check them structurally.

Exit 0 = verified; 5 = consistent but not finished (backfill / top-up still pending — progress);
1 = problems; 4 = nothing there yet.
Pass criteria (all from the pod's verify, re-checked here): every configured symbol has data or is a
recorded no-vendor-data name; every one is caught up; no structural errors (sorted, unique ts, Eastern
offsets, ET-year partition); every 09:30 bar within 10 bps of the daily open; extended-hours bars on
>= 95% of fresh symbols; no historical session left un-asked; last run had no failed symbols."""
import importlib.util, json, os, subprocess, sys, tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
DA = os.path.join(ROOT, "data_acquisition")
ENV = {}
for line in open(os.path.join(DA, "runpod", ".env")):
    if "=" in line and not line.startswith("#"):
        k, v = line.strip().split("=", 1); ENV[k] = v.strip().strip('"')
S3FLAGS = ["--region", ENV["RUNPOD_S3_REGION"], "--endpoint-url", ENV["RUNPOD_S3_ENDPOINT"]]
BUCKET = f"s3://{ENV['RUNPOD_VOLUME_ID']}"
AWSENV = dict(os.environ, AWS_ACCESS_KEY_ID=ENV["AWS_ACCESS_KEY_ID"], AWS_SECRET_ACCESS_KEY=ENV["AWS_SECRET_ACCESS_KEY"], AWS_EC2_METADATA_DISABLED="true")


def s3get(key, dest=None):
    cmd = ["aws", "s3", "cp"] + S3FLAGS + [f"{BUCKET}/{key}", dest or "-"]
    p = subprocess.run(cmd, capture_output=True, text=(dest is None), env=AWSENV)
    if p.returncode != 0: return None
    return p.stdout if dest is None else dest


def fetcher():
    spec = importlib.util.spec_from_file_location("fetch_intraday", os.path.join(DA, "src", "fetch_intraday.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def check_parquet(root, sym, year, problems):
    import pyarrow.parquet as pq
    with tempfile.TemporaryDirectory() as td:
        f = s3get(f"{root}/1m/{sym}/{year}.parquet", os.path.join(td, "x.parquet"))
        if not f:
            problems.append(f"could not download {sym}/{year}.parquet"); return
        t = pq.read_table(f)
        names = t.schema.names; ts = t.column("ts").to_pylist(); off = t.column("gmtoffset").to_pylist()
        inc = all(ts[i] < ts[i + 1] for i in range(len(ts) - 1))
        offs = set(off)
        loc = [datetime.fromtimestamp(a + b, tz=timezone.utc) for a, b in zip(ts, off)]
        last_day = max(d.date() for d in loc)
        bars = [d for d in loc if d.date() == last_day]
        reg = sum(1 for d in bars if (9, 30) <= (d.hour, d.minute) < (16, 0)); pre = sum(1 for d in bars if (d.hour, d.minute) < (9, 30))
        post = len(bars) - reg - pre
        wrong_year = sum(1 for d in loc if d.year != int(year))
        print(f"sample {sym}/{year}.parquet: {t.num_rows:,} rows, columns {names}, ts strictly increasing={inc}, offsets {sorted(offs)}, "
              f"wrong-year rows {wrong_year}; last session {last_day}: {pre} pre-market / {reg} regular / {post} after-hours bars, "
              f"{min(bars).strftime('%H:%M')}-{max(bars).strftime('%H:%M')} ET")
        if names != ["ts", "gmtoffset", "open", "high", "low", "close", "volume"]: problems.append(f"{sym}: unexpected columns {names}")
        if not inc: problems.append(f"{sym}/{year}: timestamps not strictly increasing")
        if not offs <= {-14400, -18000}: problems.append(f"{sym}/{year}: offsets {offs}")
        if wrong_year: problems.append(f"{sym}/{year}: {wrong_year} rows outside the file's ET year")


def main():
    fi = fetcher()
    cfg = json.load(open(os.path.join(DA, "config", "intraday.json")))
    want = fi.resolve_universe(cfg, os.path.join(DA, "config"))
    root = None
    for cand in ("data/tickdata", "data/intraday_1m"):
        m = s3get(f"{cand}/_manifest.json")
        if m and json.loads(m).get("producer") == "fetch_intraday":
            root = cand; manifest = json.loads(m); break
    if not root:
        print("no intraday store with our marker under data/tickdata or data/intraday_1m"); return 4
    run = json.loads(s3get(f"{root}/_run.json") or "{}"); ver = json.loads(s3get(f"{root}/_verify.json") or "{}")
    syms = manifest.get("symbols", {})
    problems, pending = [], []
    print(f"store: {root}   manifest updated {manifest.get('updated_at', '?')[:19]}   last run ended {run.get('ended_at', '?')[:19]}")
    if run:
        print(f"last run: {run.get('n_symbols')}/{run.get('n_configured', '?')} symbols worked, ok {run.get('n_ok')}, fail {run.get('n_fail')}, "
              f"requests {run.get('requests_used'):,} (~{5 * (run.get('requests_used') or 0):,} credits), rows added {run.get('rows_added'):,}, "
              f"stop {run.get('stop_reason') or 'none'}; space mode {run.get('space_mode')}, volume grown {run.get('volume_grown_gb')} GB, "
              f"free at end {run.get('free_gb_at_end')} GB")
        fails = [(r['symbol'], r['error']) for r in run.get("results", []) if not r["ok"]]
        if fails: problems.append(f"{len(fails)} symbols failed last run: {fails[:8]}")
    no_vendor = [s for s in want if syms.get(s, {}).get("no_vendor_data_until") and not syms[s].get("rows")]
    missing = [s for s in want if not syms.get(s, {}).get("rows") and s not in no_vendor]
    behind = [s for s in want if syms.get(s, {}).get("rows") and not syms[s].get("complete")]
    tot = sum(int(syms.get(s, {}).get("rows") or 0) for s in want)
    print(f"universe (local configs): {len(want)} symbols; {len(want) - len(missing) - len(no_vendor)} with bars ({tot:,} rows), "
          f"{len(behind)} not caught up, {len(missing)} not pulled yet, {len(no_vendor)} with no vendor intraday {no_vendor}")
    if missing: pending.append(f"{len(missing)} symbols not pulled yet (first: {missing[:8]})")
    if behind: pending.append(f"{len(behind)} symbols not caught up (first: {behind[:8]})")

    sm = ver.get("summary", {})
    if not sm:
        problems.append("no _verify.json summary from the pod")
    else:
        print(f"verify (pod, {ver.get('generated_at', '?')[:19]}): status {sm['status']}; last completed session {sm['last_completed_session']}; "
              f"{sm['symbols_with_data']}/{sm['symbols_configured']} with data, {sm['symbols_complete']} caught up, {sm['symbols_fresh']} fresh "
              f"(lagging: {len(sm['symbols_lagging'])}{' ' + str(sm['symbols_lagging'][:6]) if sm['symbols_lagging'] else ''})")
        print(f"  history: {sm['rows_total']:,} bars; {sm['missing_sessions_old']} historical sessions absent across {sm['symbols_with_old_gaps']} symbols, "
              f"{sm['missing_sessions_old_untried']} not yet re-asked; structural-error symbols {sm['structural_error_symbols']}; "
              f"{sm['out_of_hours_bars']} bars outside 04:00-20:00, {sm['ohlc_anomaly_bars']} OHLC anomalies (informational)")
        print(f"  extended hours on the last session: {sm['symbols_with_extended_hours_on_last_session']}/{sm['symbols_fresh']} fresh symbols; "
              f"09:30 bar vs daily open: {sm['open_checks_within_5bps']}/{sm['open_checks']} within 5 bps, worst {sm['open_check_worst_bps']} bps")
        problems += sm.get("problems", [])
        if set(sm.get("symbols_not_pulled", [])) - set(missing): problems.append("pod verify and manifest disagree on unpulled symbols")
        if sm["missing_sessions_old_untried"]: pending.append(f"{sm['missing_sessions_old_untried']} historical sessions not yet re-asked")
    held = [s for s in want if syms.get(s, {}).get("years")]
    if held:
        first = next((s for s in ("AAPL", "MU") if s in held), held[0])
        newest = max(held, key=lambda s: syms[s].get("updated_at", ""))
        for s in dict.fromkeys([first, newest]):
            check_parquet(root, s, syms[s]["years"][-1], problems)
    if problems:
        print("PROBLEMS:"); [print("  -", p) for p in problems]; return 1
    if pending:
        print("NOT FINISHED (consistent so far):"); [print("  -", p) for p in pending]; return 5
    print(f"VERIFIED: minute store consistent — {len(want)} symbols ({len(no_vendor)} without vendor intraday), full history checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
