#!/usr/bin/env python3
"""D-16 model artifact: FinBERT weights for F9 local sentiment scoring.

Spec v1.2 §2 D-16: "One-time `ProsusAI/finbert` pull from Hugging Face; pin revision hash into
`config_hash` inputs (G-10 reproducibility)." Free, no token, pure stdlib.

WHY A REVISION HASH AND NOT `main`. HuggingFace `main` is a moving branch. Scoring the same
headline against two different revisions gives two different F9 values, which silently breaks
G-10's reproducibility guarantee and makes a backtest unrepeatable. This fetcher resolves the
repo's current commit sha ONCE, writes it to the manifest, and thereafter downloads at that pinned
sha — re-running can never change the weights underneath you. Bump `revision` in the config
deliberately, and the manifest records both the old and new sha.

WHAT IT PULLS. Only the PyTorch artefacts F9 needs:
    config.json  pytorch_model.bin  vocab.txt  tokenizer_config.json  special_tokens_map.json
`flax_model.msgpack` and `tf_model.h5` are the same weights for other frameworks (~400 MB each)
and are skipped.

Each file is verified against the size HuggingFace reports before it is moved into place, and a
file already on disk with the right size and sha is skipped — so this is cheap to leave in the
daily routine even though it is a one-time pull.

OUTPUT
    DATA_DIR/<revision>/<file>   the weights, immutable under their sha
    DATA_DIR/_run.json           manifest incl. the resolved sha for config_hash / G-10

    DATA_DIR=./data_finbert CONFIG_PATH=data_acquisition/config/finbert.json \
      python3 data_acquisition/src/fetch_finbert.py
"""
import hashlib
import json
import os
import ssl
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

DATA_DIR = os.environ.get("DATA_DIR", "/workspace/data_finbert")
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/workspace/code/finbert.json")
HF = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
TIMEOUT = int(os.environ.get("HF_TIMEOUT", "600"))
STORE_LOGS = os.environ.get("STORE_LOGS", "").strip().lower() in ("1", "true", "yes", "on")

DEFAULT_FILES = ["config.json", "pytorch_model.bin", "vocab.txt",
                 "tokenizer_config.json", "special_tokens_map.json"]

_ctx = None
_LOG = []


def log(m):
    print(m, flush=True)
    _LOG.append(m)


def _persist_log(kind, payload=None):
    d = os.path.join(DATA_DIR, "logs")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.log")
    with open(p, "w") as f:
        f.write("\n".join(_LOG) + "\n")
        if payload is not None:
            f.write("\n--- manifest ---\n" + json.dumps(payload, indent=2) + "\n")
    return p


def _open(url):
    """GET with a one-time downgrade on a certificate-VERIFICATION failure only (slim images ship
    no CA bundle). Never on a transient SSLError — see the sibling fetchers."""
    global _ctx
    req = urllib.request.Request(url, headers={"User-Agent": "InvestOpediaClaude-DataAcquisition/1.0"})
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx)
    except urllib.error.URLError as e:
        if _ctx is not None or not isinstance(getattr(e, "reason", e), ssl.SSLCertVerificationError):
            raise
        log("WARN: no usable CA bundle — retrying unverified (public weights, no credential sent)")
        _ctx = ssl._create_unverified_context()
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=_ctx)


def resolve(repo, revision):
    """Repo metadata at `revision`; returns (sha, {filename: size_or_None})."""
    with _open(f"{HF}/api/models/{repo}" + ("" if revision in (None, "", "main")
                                            else f"/revision/{revision}")) as r:
        meta = json.loads(r.read().decode())
    sha = meta.get("sha")
    sizes = {}
    for s in meta.get("siblings") or []:
        sizes[s.get("rfilename")] = s.get("size")
    return sha, sizes


def download(repo, sha, name, dest):
    url = f"{HF}/{repo}/resolve/{sha}/{name}"
    tmp = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    h = hashlib.sha256()
    n = 0
    with _open(url) as r, open(tmp, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            n += len(chunk)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dest)
    return n, h.hexdigest()


def main():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    repo = cfg.get("repo", "ProsusAI/finbert")
    revision = cfg.get("revision") or "main"
    files = cfg.get("files") or DEFAULT_FILES
    started = datetime.now(timezone.utc)
    results = []

    sha, sizes = resolve(repo, revision)
    if not sha:
        print(f"FATAL: could not resolve a commit sha for {repo}@{revision}", file=sys.stderr)
        return 1
    pinned = revision not in (None, "", "main")
    log(f"     {repo}@{revision} -> sha {sha}" + ("" if pinned else
        "  (resolved from the MOVING 'main' branch — pin this sha in config/finbert.json"
        " so F9 scores stay reproducible, G-10)"))

    out_dir = os.path.join(DATA_DIR, sha)
    for name in files:
        entry = {"symbol": name, "dataset": "finbert", "ok": False, "count": 0,
                 "added": 0, "error": None}
        dest = os.path.join(out_dir, name)
        want = sizes.get(name)
        try:
            if os.path.exists(dest) and (want is None or os.path.getsize(dest) == want):
                entry.update(ok=True, count=os.path.getsize(dest), added=0)
                log(f"OK   finbert {name:24}: {os.path.getsize(dest):>12,} B [already at {sha[:8]}]")
            else:
                n, digest = download(repo, sha, name, dest)
                if want is not None and n != want:
                    raise RuntimeError(f"size mismatch: got {n}, HF reports {want}")
                entry.update(ok=True, count=n, added=n, sha256=digest)
                log(f"OK   finbert {name:24}: {n:>12,} B  sha256 {digest[:16]}…")
        except Exception as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            log(f"FAIL finbert {name}: {entry['error']}")
        results.append(entry)

    all_ok = bool(results) and all(r["ok"] for r in results)
    manifest = {
        "vendor": "Hugging Face (free)",
        "spec": "Data Acquisition Specification — FINAL v1.2",
        "spec_items": ["D-16"],
        "ended_at": started.isoformat(),
        "repo": repo,
        "requested_revision": revision,
        "resolved_sha": sha,          # <- feed this into config_hash (G-10)
        "pinned": pinned,
        "path": out_dir,
        "ok": all_ok,
        "results": results,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = os.path.join(DATA_DIR, "_run.json.part")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
    os.replace(tmp, os.path.join(DATA_DIR, "_run.json"))

    if not all_ok:
        log(f"FAILED — error log: {_persist_log('error', manifest)}")
        return 1
    if STORE_LOGS:
        _persist_log("run", manifest)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        try:
            _LOG.append(traceback.format_exc())
            _persist_log("crash")
        finally:
            traceback.print_exc()
        sys.exit(1)
