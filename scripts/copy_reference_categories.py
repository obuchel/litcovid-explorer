"""
copy_reference_categories.py
------------------------------
One-time merge: pulls `cat` / `hard_category` / `format` out of the
whn-analytics.net reference JSON (php2_category_xavi_last0.json) for any
PMID this repo also has, and copies those three fields into
data/mesh_category_tree.json's docs[] wherever this repo's own value is
still blank.

Why just these three: build_mesh_annotations.py already established (and a
manual check of the reference file confirmed) that art_id / yearR-yearJI /
lang / doi2 simply don't exist in the reference docs[] at all — those come
from some other manual/automatic process this project doesn't have. cat /
hard_category / format are a different case: this pipeline also leaves them
blank today, but if the reference project's classification step assigned
real values for documents whose PMIDs overlap with this repo's, they're
copyable 1:1 — there's no fabrication involved, just filling in a value
this repo already has a slot for.

The reference file is large, so it's downloaded once (cached, not
committed — see .gitignore) and stream-parsed with ijson rather than loaded
into memory whole. Only PMIDs already present in this repo's docs[] are
looked up; only non-empty reference values are copied; an existing non-blank
value in this repo is never overwritten.

After merging, this also rebuilds the top-level `categories` / `hard_category`
/ `formats` parallel arrays (one entry per doc, same shape as the existing
`factors`/`citations` arrays build_mesh_annotations.py produces) from the
current state of docs[] — so they stay a true reflection of what's in docs[]
even across repeated runs, not just a one-time snapshot.

Requires: pip install ijson

Usage:
    python scripts/copy_reference_categories.py
    python scripts/copy_reference_categories.py --limit 200      # test run
    python scripts/copy_reference_categories.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from typing import Any

try:
    import ijson
except ImportError:
    ijson = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DATA_DIR = os.path.join(BASE_DIR, "data")
SCRATCH_DIR = os.path.join(BASE_DIR, ".mesh_cache")  # shared with build_mesh_annotations.py, never committed

DEFAULT_TREE_JSON = os.path.join(DATA_DIR, "mesh_category_tree.json")
DEFAULT_REFERENCE_URL = "https://whn-analytics.net/whn/php2_category_xavi_last0.json"
DEFAULT_REFERENCE_CACHE = os.path.join(SCRATCH_DIR, "whn_reference_last0.json")

FIELDS = ("cat", "hard_category", "format")

MAX_GIT_FILE_BYTES = 90 * 1024 * 1024  # stay under GitHub's hard 100MB push limit with headroom


def git_checkpoint(*paths: str, message: str) -> None:
    """Same safety net as the other scripts in this pipeline. Never raises."""
    try:
        existing = []
        for p in paths:
            if not os.path.exists(p):
                continue
            size = os.path.getsize(p)
            if size > MAX_GIT_FILE_BYTES:
                print(f"WARNING: {p} is {size / 1024 / 1024:.1f}MB, over the safe limit — skipping this checkpoint", flush=True)
                continue
            existing.append(p)
        if not existing:
            return
        subprocess.run(["git", "add", *existing], cwd=BASE_DIR, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if diff.returncode == 0:
            return
        subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        print(f"Checkpoint committed: {message}", flush=True)
    except subprocess.CalledProcessError as exc:
        print(f"Checkpoint commit failed (continuing): {exc}", flush=True)


def ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def download_reference(url: str, dest_path: str, force_refresh: bool = False) -> str:
    if force_refresh and os.path.exists(dest_path):
        os.remove(dest_path)
    if os.path.exists(dest_path):
        print(f"Using cached reference download: {dest_path}")
        return dest_path
    ensure_parent_dir(dest_path)
    print(f"Downloading {url} ...")
    urllib.request.urlretrieve(url, dest_path)
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"Saved {dest_path} ({size_mb:.1f} MB)")
    return dest_path


def stream_reference_matches(path: str, wanted_pmids: set[str]) -> dict[str, dict[str, str]]:
    """Stream-parses the reference file's docs[] array (via ijson, so the
    whole file is never loaded into memory at once — it's large), returning
    {pmid: {"cat", "hard_category", "format"}} for every wanted PMID whose
    reference record has at least one non-empty value among those three."""
    if ijson is None:
        raise RuntimeError("This script needs the 'ijson' package: pip install ijson")
    result: dict[str, dict[str, str]] = {}
    with open(path, "rb") as fp:
        for doc in ijson.items(fp, "docs.item"):
            pmid = str(doc.get("pmid", ""))
            if pmid not in wanted_pmids:
                continue
            values = {field: (doc.get(field) or "") for field in FIELDS}
            if any(values.values()):
                result[pmid] = values
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree-json", default=DEFAULT_TREE_JSON)
    parser.add_argument("--reference-url", default=DEFAULT_REFERENCE_URL)
    parser.add_argument("--reference-cache", default=DEFAULT_REFERENCE_CACHE, help="Where the (large) downloaded reference JSON is cached")
    parser.add_argument("--limit", type=int, help="Only check the first N of this repo's PMIDs, for testing")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing tree-json or committing")
    parser.add_argument("--force-refresh", action="store_true", help="Ignore the cached reference download and re-fetch")
    parser.add_argument("--checkpoint-every", type=int, default=0, help="Unused placeholder for symmetry with the other scripts (this is a single-pass merge, not batched)")
    parser.add_argument("--no-checkpoint-commit", action="store_true", help="Write tree-json to disk but skip git commit/push")
    args = parser.parse_args()

    if not os.path.exists(args.tree_json):
        print(f"ERROR: {args.tree_json} not found.", flush=True)
        return

    with open(args.tree_json, "r", encoding="utf-8") as fp:
        tree_data = json.load(fp)
    docs: list[dict[str, Any]] = tree_data.get("docs", [])
    docs_by_pmid = {d["pmid"]: d for d in docs if d.get("pmid")}

    pmids = list(docs_by_pmid.keys())
    if args.limit:
        pmids = pmids[: args.limit]
    wanted = set(pmids)
    print(f"{len(wanted)} PMIDs in this repo to check against the reference", flush=True)

    ref_path = download_reference(args.reference_url, args.reference_cache, force_refresh=args.force_refresh)
    matches = stream_reference_matches(ref_path, wanted)
    print(f"Reference has a non-empty cat/hard_category/format for {len(matches)} of those PMIDs", flush=True)

    updated = 0
    field_fill_counts = {f: 0 for f in FIELDS}
    for pmid, values in matches.items():
        doc = docs_by_pmid[pmid]
        changed = False
        for field in FIELDS:
            if values[field] and not doc.get(field):
                doc[field] = values[field]
                field_fill_counts[field] += 1
                changed = True
        if changed:
            updated += 1

    print(f"Would update {updated} documents (dry run)" if args.dry_run else f"Updated {updated} documents", flush=True)
    for field, count in field_fill_counts.items():
        print(f"  {field}: filled in for {count} documents", flush=True)

    if args.dry_run:
        return

    tree_data["docs"] = docs
    # Parallel arrays, rebuilt from the current (just-updated) docs[] — same
    # convention build_mesh_annotations.py already uses for factors/citations.
    # "categories" is new here (mirrors doc.cat); hard_category/formats match
    # the existing key names in this schema. Rebuilt from ALL of docs[], not
    # just the PMIDs this run touched, so they stay a true reflection of
    # current state even across repeated runs.
    tree_data["categories"] = [{"cat": d.get("cat", "")} for d in docs]
    tree_data["hard_category"] = [{"hard_category": d.get("hard_category", "")} for d in docs]
    tree_data["formats"] = [{"format": d.get("format", "")} for d in docs]

    ensure_parent_dir(args.tree_json)
    with open(args.tree_json, "w", encoding="utf-8") as fp:
        json.dump(tree_data, fp, ensure_ascii=False, indent=2)
    print(f"Wrote updated {args.tree_json}", flush=True)

    if not args.no_checkpoint_commit:
        git_checkpoint(args.tree_json, message=f"Copy cat/hard_category/format from reference for {updated} PMIDs [skip ci]")


if __name__ == "__main__":
    main()
