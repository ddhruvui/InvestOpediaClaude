#!/usr/bin/env python3
"""One-shot: move this repo's state from the volume ROOT into results/InvestOpediaClaude/.

Until 2026-09-17 the models wrote m1x/, derived/, models/, ledger/, reports/, their pod logs
and unpacked bundles straight into the volume root, mixed in with the DataAcquistion trees.
Everything this repo writes now lives under results/InvestOpediaClaude/ (RESULTS_PREFIX in
src/config.py and scripts/_common.sh) and the root is only read. Moving the existing state —
rather than letting the next run start empty — keeps the champions (a cold store forces a
full refit), m1x's resumable _built.json (a missing one re-parses every year), the G-09
ledger (DSR's N must never reset) and the stage reports the console is built from.

Runs ON A POD (scripts/launch_predict.sh migrate), where a move is a same-filesystem rename:
no bytes copied, no quota needed. If rename ever reports a cross-device move it falls back
file by file, so the extra space in use at any moment is one file, never a whole tree.

Nothing is deleted and nothing is overwritten: a destination that already holds a file of
the same name is a FAIL for that item, left for a human. Superseded code copies and unpack
scratch go to results/InvestOpediaClaude/_pre_migration/. Every moved item is verified
(same relative paths, same sizes, source gone). Idempotent: a re-run finds nothing and
exits 0. The last line is `migrate=<0|1>`.

    python tools/migrate_results_layout.py --root /workspace [--dry-run]
"""
from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import sys
from pathlib import Path

RESULTS_PREFIX = "results/InvestOpediaClaude"      # = src.config.RESULTS_PREFIX
TREES = ["m1x", "derived", "models", "ledger", "reports"]
LEGACY = {"code/predict": "_pre_migration/code/predict", "code/sync": "_pre_migration/code/sync"}
SCRATCH = re.compile(r"predict_src_[A-Za-z0-9_]+")
# This repo's pods only: <ts>-predict-<job>-<podid>.log and <ts>-sync-<podid>.log. The
# fetchers' <ts>-<script>.py-<podid>.log stay where DataAcquistion reads them.
OUR_LOG = re.compile(r"\d{8}T\d{6}Z-(?:predict-[a-z0-9]+|sync)-([a-z0-9]+)\.log")


def inventory(p: Path) -> dict[str, int]:
    if not p.is_dir():
        return {"": p.stat().st_size}
    out = {}
    for dirpath, _, files in os.walk(p):
        for f in files:
            fp = Path(dirpath) / f
            out[str(fp.relative_to(p))] = fp.stat().st_size
    return out


def move(src: Path, dst: Path) -> None:
    """Rename src -> dst, merging into an existing directory; never overwrite a file."""
    if dst.exists():
        if not (src.is_dir() and dst.is_dir()):
            raise FileExistsError(f"{dst} already exists")
        for child in sorted(src.iterdir()):
            move(child, dst / child.name)
        src.rmdir()
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        if src.is_dir():
            dst.mkdir()
            for child in sorted(src.iterdir()):
                move(child, dst / child.name)
            src.rmdir()
        else:
            shutil.copy2(src, dst)
            os.unlink(src)


def plan(root: Path) -> list[tuple[Path, Path]]:
    res = root / RESULTS_PREFIX
    items = [(root / t, res / t) for t in TREES]
    items += [(root / s, res / d) for s, d in LEGACY.items()]
    items += [(p, res / "_pre_migration" / p.name)
              for p in sorted(root.iterdir()) if p.is_dir() and SCRATCH.fullmatch(p.name)]
    logs = root / "_pod_logs"
    if logs.is_dir():
        ours = [(p, OUR_LOG.fullmatch(p.name)) for p in sorted(logs.iterdir())]
        pods = {m.group(1) for _, m in ours if m}
        items += [(p, res / "_pod_logs" / p.name) for p, m in ours if m]
        items += [(logs / f".ran-{pid}", res / "_pod_logs" / f".ran-{pid}") for pid in sorted(pods)]
    return [(s, d) for s, d in items if s.exists()]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default="/workspace", help="volume mount point")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    rel = lambda p: str(p.relative_to(root))

    items = plan(root)
    if not items:
        print(f"migrate: nothing of this repo's left at the root of {root} — already migrated")
        print("migrate=0")
        return 0
    failed = 0
    for src, dst in items:
        before = inventory(src)
        what = f"{rel(src)} -> {rel(dst)}  ({len(before)} files, {sum(before.values()):,} bytes)"
        if args.dry_run:
            print(f"migrate: would move {what}")
            continue
        try:
            move(src, dst)
        except OSError as e:
            print(f"migrate: FAIL {what}: {e}")
            failed += 1
            continue
        after = inventory(dst)
        bad = [k for k, size in before.items() if after.get(k) != size]
        if bad or src.exists():
            print(f"migrate: FAIL {what}: {len(bad)} files missing or resized at the "
                  f"destination, source still present={src.exists()}")
            failed += 1
        else:
            print(f"migrate: OK   {what}")
    print(f"migrate={int(failed > 0)} ({len(items) - failed} of {len(items)} moved"
          f"{', DRY RUN' if args.dry_run else ''})")
    return int(failed > 0)


if __name__ == "__main__":
    sys.exit(main())
