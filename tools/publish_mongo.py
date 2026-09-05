#!/usr/bin/env python3
"""Publish a report bundle to MongoDB so the deployed console can serve it.

    python3 tools/publish_mongo.py --bundle /workspace/reports/latest        # on the pod (bundle "latest")
    python3 tools/publish_mongo.py --bundle <tmp>/reports/h60 --name h60     # a named era
    python3 tools/publish_mongo.py --bundle <dir> --dry-run                  # show what would change
    python3 tools/publish_mongo.py --bundle <dir> --backfill-git             # + every past book git remembers

Runs right after tools/build_reports.py — normally on the predict pod
(pod_bootstrap_predict.sh publish_bundle), or from scripts/refresh_console.sh.
The bundle directory's *.json files are the whole contract with the UI, and
this copies them verbatim — nothing is recomputed. *.md files next to them
(the human-readable book) are stored as text, so nothing needs to stay local.

Credentials: MONGO_URI and DB_PASSWORD from the environment, else from .env at
the repo root, else data_acquisition/runpod/.env. A literal <db_password>
placeholder in MONGO_URI is replaced with the URL-encoded DB_PASSWORD.

Database InvestOpediaClaude (override with MONGO_DB):
    reports      _id "<bundle>/<section>"       current bundle, one doc per section
    predictions  _id "<bundle>/<as_of_close>"   one doc per published book (history)
    publishes    one doc per run of this script (audit trail)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = "InvestOpediaClaude"
ENV_FILES = [ROOT / ".env", ROOT / "data_acquisition" / "runpod" / ".env"]


def load_env_files() -> None:
    """KEY=VALUE lines; the process environment always wins."""
    for f in ENV_FILES:
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)


def resolve_uri() -> str:
    uri = os.environ.get("MONGO_URI")
    if not uri:
        sys.exit("MONGO_URI not set (env, .env, or data_acquisition/runpod/.env)")
    if "<db_password>" in uri:
        pw = os.environ.get("DB_PASSWORD")
        if pw is None:
            sys.exit("MONGO_URI has a <db_password> placeholder but DB_PASSWORD is not set")
        uri = uri.replace("<db_password>", urllib.parse.quote_plus(pw))
    return uri


def connect(uri: str):
    try:
        import pymongo
    except ImportError:
        sys.exit("pymongo missing: pip install pymongo certifi")
    kwargs = {"serverSelectionTimeoutMS": 20000, "appname": "publish_mongo"}
    try:  # python.org builds on macOS ship without a CA bundle wired into ssl
        import certifi
        kwargs["tlsCAFile"] = certifi.where()
    except ImportError:
        pass
    client = pymongo.MongoClient(uri, **kwargs)
    client.admin.command("ping")
    return client


def backfill_git(preds, bundle_dir: Path, name: str, now: str) -> int:
    """Every committed version of <bundle>/suggestions.json becomes a predictions
    row, unless that as_of_close was already published (a real publish wins)."""
    import subprocess
    rel = str(bundle_dir / "suggestions.json")
    log = subprocess.run(["git", "log", "--format=%H %cI", "--", rel], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    added = 0
    for sha, committed in zip(log[0::2], log[1::2]):        # newest first
        blob = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=ROOT,
                              capture_output=True, text=True)
        if blob.returncode:
            continue
        try:
            sug = json.loads(blob.stdout)
        except ValueError:
            continue
        as_of = sug.get("as_of_close")
        if not as_of or preds.find_one({"_id": f"{name}/{as_of}"}, {"_id": 1}):
            continue
        preds.insert_one({
            "_id": f"{name}/{as_of}", "bundle": name, "as_of_close": as_of,
            "built_utc": None, "published_utc": now, "committed_utc": committed,
            "source": f"git:{sha[:12]}",
            "config_hash": (sug.get("stamp") or {}).get("config_hash"),
            "verdict": None,
            "n_buys": len(sug.get("buys_or_increases") or []),
            "n_holds": len(sug.get("holds") or []),
            "n_sells": len(sug.get("sells_or_exits") or []),
            "tickers": [r.get("ticker") for r in sug.get("buys_or_increases") or []],
            "data": sug,
        })
        added += 1
        print(f"  backfilled {as_of} from {sha[:12]} ({committed[:10]})")
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bundle", required=True, help="bundle directory (from tools/build_reports.py)")
    ap.add_argument("--name", default=None,
                    help="bundle name in Mongo (default: the directory's basename)")
    ap.add_argument("--db", default=None, help=f"database (default {DEFAULT_DB})")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backfill-git", action="store_true",
                    help="also add every past suggestions.json in git history to predictions")
    args = ap.parse_args()

    bundle_dir = Path(args.bundle)
    if not bundle_dir.is_dir():
        sys.exit(f"no such bundle directory: {bundle_dir}")
    name = args.name or bundle_dir.name
    files = sorted(bundle_dir.glob("*.json"))
    if not files:
        sys.exit(f"no *.json in {bundle_dir} — run tools/build_reports.py first")
    md_files = sorted(bundle_dir.glob("*.md"))

    load_env_files()
    db_name = args.db or os.environ.get("MONGO_DB") or DEFAULT_DB

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sections: dict[str, dict] = {}
    for f in files:
        raw = f.read_bytes()
        try:
            data = json.loads(raw)
        except ValueError as e:
            sys.exit(f"{f}: not valid JSON ({e})")
        sections[f.stem] = {"data": data, "sha256": hashlib.sha256(raw).hexdigest(),
                            "size_bytes": len(raw)}

    for f in md_files:
        raw = f.read_bytes()
        sections[f.stem] = {"text": raw.decode("utf-8", "replace"),
                            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}

    manifest = sections.get("manifest", {}).get("data", {})
    built_utc = manifest.get("built_utc")
    sug = sections.get("suggestions", {}).get("data")
    summ = sections.get("summary", {}).get("data") or {}
    as_of = sug.get("as_of_close") if sug else None
    verdict = (summ.get("gates") or {}).get("verdict")

    print(f"bundle {bundle_dir} -> {db_name} as '{name}'  built={built_utc} "
          f"as_of_close={as_of} verdict={verdict}")
    for sec, s in sections.items():
        print(f"  {sec:16s} {s['size_bytes']:>10,} B  {s['sha256'][:12]}")
    if args.dry_run:
        print("dry run — nothing written")
        return 0

    client = connect(resolve_uri())
    db = client[db_name]
    reports = db["reports"]
    preds = db["predictions"]
    reports.create_index([("bundle", 1), ("section", 1)])
    preds.create_index([("bundle", 1), ("as_of_close", -1)])

    ops = {"inserted": 0, "updated": 0, "unchanged": 0}
    for sec, s in sections.items():
        doc_id = f"{name}/{sec}"
        prev = reports.find_one({"_id": doc_id}, {"sha256": 1})
        doc = {"_id": doc_id, "bundle": name, "section": sec,
               "sha256": s["sha256"], "size_bytes": s["size_bytes"],
               "built_utc": built_utc, "published_utc": now,
               "source_host": socket.gethostname()}
        if "data" in s:
            doc["data"] = s["data"]
        else:
            doc["text"] = s["text"]; doc["kind"] = "markdown"
        if sec == "suggestions":
            doc["as_of_close"] = as_of
        reports.replace_one({"_id": doc_id}, doc, upsert=True)
        if prev is None:
            ops["inserted"] += 1
        elif prev.get("sha256") == s["sha256"]:
            ops["unchanged"] += 1
        else:
            ops["updated"] += 1
    # sections that vanished from the bundle must not linger as stale data
    gone = reports.delete_many({"bundle": name, "section": {"$nin": list(sections)}})

    if sug and as_of:
        preds.replace_one({"_id": f"{name}/{as_of}"}, {
            "_id": f"{name}/{as_of}", "bundle": name, "as_of_close": as_of,
            "built_utc": built_utc, "published_utc": now,
            "config_hash": (sug.get("stamp") or {}).get("config_hash"),
            "verdict": verdict,
            "n_buys": len(sug.get("buys_or_increases") or []),
            "n_holds": len(sug.get("holds") or []),
            "n_sells": len(sug.get("sells_or_exits") or []),
            "tickers": [r.get("ticker") for r in sug.get("buys_or_increases") or []],
            "data": sug,
        }, upsert=True)

    if args.backfill_git:
        print("backfilling prediction history from git:")
        n = backfill_git(preds, bundle_dir, name, now)
        print(f"  {n} past book(s) added")

    db["publishes"].insert_one({
        "bundle": name, "published_utc": now, "built_utc": built_utc,
        "as_of_close": as_of, "verdict": verdict, "sections": list(sections),
        "bytes": sum(s["size_bytes"] for s in sections.values()),
        "host": socket.gethostname(), **ops,
    })

    # read back: the deployed console sees exactly this
    n_reports = reports.count_documents({"bundle": name})
    n_preds = preds.count_documents({"bundle": name})
    check = reports.find_one({"_id": f"{name}/suggestions"}, {"as_of_close": 1})
    print(f"published: {ops['inserted']} new, {ops['updated']} updated, "
          f"{ops['unchanged']} unchanged, {gone.deleted_count} stale removed")
    print(f"verify: reports={n_reports} docs for '{name}', predictions history={n_preds}, "
          f"suggestions.as_of_close={check.get('as_of_close') if check else None}")
    if not check or (as_of and check.get("as_of_close") != as_of):
        print("FATAL: read-back mismatch", file=sys.stderr)
        return 1
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
