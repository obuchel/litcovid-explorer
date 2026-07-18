"""
split_category_trees.py
-------------------------
Splits a combined mesh_category_tree.json (or mesh_category_tree_predicted.json
— any file with this schema) into separate files per top-level MeSH category,
mirroring how whn-analytics.net splits php2_category_xavi_last0.json
(Diseases-only) from php2_category_xavi_last_chemical.json (Chemicals-only).

Unlike the reference site, this script does NOT just duplicate docs[] across
every output file — each category's docs[] is trimmed down to only the
documents that actually mention at least one term from THAT category's tree
(a doc that only ever mentions chemicals doesn't belong in the diseases
file). tree[] and docs[] stay a matched pair in every output file, the way
the rest of this pipeline already treats them.

By default, produces:
  - mesh_category_tree_diseases.json       (MeSH top-level "Diseases")
  - mesh_category_tree_chemicals.json      (MeSH top-level "Chemicals and Drugs")
  - mesh_category_tree_other.json          (everything else this pipeline
                                             resolves: Psychiatry and
                                             Psychology, Anatomy, Phenomena
                                             and Processes, etc. — small
                                             branches, lumped together
                                             rather than one tiny file each)

Pass --split-all for one file per distinct top-level category instead of
lumping the small ones into "other".

Genes are NOT part of this split — MeSH doesn't classify genes at all (they
come from PubTator as NCBI Entrez Gene IDs, a different ID space entirely),
and this pipeline doesn't currently capture Gene-type PubTator annotations
in the first place (see build_mesh_annotations.py's
MESH_RESOLVABLE_CATEGORIES = {"Disease", "Chemical"}). A genes file, if
wanted, would need its own extraction script and its own (non-tree, flat
gene -> documents) shape — see extract_genes.py.

Usage:
    python scripts/split_category_trees.py
    python scripts/split_category_trees.py --tree-json data/mesh_category_tree_predicted.json --out-prefix data/mesh_category_tree_predicted
    python scripts/split_category_trees.py --split-all
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_TREE_JSON = os.path.join(DATA_DIR, "mesh_category_tree.json")
DEFAULT_OUT_PREFIX = os.path.join(DATA_DIR, "mesh_category_tree")

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

# Categories lumped into "other" unless --split-all is passed.
LUMP_INTO_OTHER = {"Psychiatry and Psychology", "Anatomy", "Phenomena and Processes",
                    "Technology, Industry, Agriculture", "Health Care", "Organisms",
                    "Analytical, Diagnostic and Therapeutic Techniques and Equipment"}

CATEGORY_SLUGS = {
    "Diseases": "diseases",
    "Chemicals and Drugs": "chemicals",
}


def slugify(name: str) -> str:
    return CATEGORY_SLUGS.get(name) or name.lower().replace(",", "").replace(" ", "_")


def top_level(path: str) -> str:
    return path.split(".", 1)[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree-json", default=DEFAULT_TREE_JSON)
    parser.add_argument("--out-prefix", default=DEFAULT_OUT_PREFIX, help="Output files are <prefix>_<category>.json")
    parser.add_argument("--split-all", action="store_true", help="One file per distinct top-level category, instead of lumping small ones into 'other'")
    parser.add_argument("--no-checkpoint-commit", action="store_true", help="Write the output files to disk but skip git commit/push")
    args = parser.parse_args()

    if not os.path.exists(args.tree_json):
        print(f"ERROR: {args.tree_json} not found.", flush=True)
        return

    with open(args.tree_json, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    tree: list[dict[str, Any]] = data.get("tree", [])
    docs: list[dict[str, Any]] = data.get("docs", [])
    print(f"Loaded {len(tree)} tree entries, {len(docs)} docs from {args.tree_json}", flush=True)

    # Bucket tree entries by top-level category (or by the lumped "other" bucket).
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in tree:
        cat = top_level(entry.get("first", ""))
        bucket = cat if (args.split_all or cat not in LUMP_INTO_OTHER) else "other"
        buckets[bucket].append(entry)

    # leaf name -> set of buckets it appears in (a leaf can appear in more
    # than one tree branch, in principle in more than one bucket).
    leaf_to_buckets: dict[str, set[str]] = defaultdict(set)
    for bucket, entries in buckets.items():
        for entry in entries:
            leaf = entry["first"].rsplit(".", 1)[-1]
            leaf_to_buckets[leaf].add(bucket)

    def doc_leaves(doc: dict[str, Any]) -> list[str]:
        return [s.strip() for s in (doc.get("subjects") or "").split("|") if s.strip()]

    written = []
    for bucket, entries in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        bucket_docs = [d for d in docs if any(bucket in leaf_to_buckets.get(leaf, ()) for leaf in doc_leaves(d))]
        out_path = f"{args.out_prefix}_{slugify(bucket)}.json"
        out_data = {
            "tree": entries,
            "docs": bucket_docs,
            "factors": [{"factor": d.get("factor", "")} for d in bucket_docs],
            "citations": [{"number_citations": d.get("number_citations", "")} for d in bucket_docs],
            "hard_category": [{"hard_category": d.get("hard_category", "")} for d in bucket_docs],
            "formats": [{"format": d.get("format", "")} for d in bucket_docs],
        }
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(out_data, fp, ensure_ascii=False, indent=2)
        print(f"{bucket}: {len(entries)} tree entries, {len(bucket_docs)} docs -> {out_path}", flush=True)
        written.append(out_path)

    print(f"\nWrote {len(written)} files.", flush=True)

    if not args.no_checkpoint_commit:
        git_checkpoint(*written, message=f"Split category trees ({', '.join(buckets.keys())}) [skip ci]")
    print("\nNOTE: no genes file produced — MeSH doesn't classify genes, and this "
          "pipeline doesn't currently capture Gene-type PubTator annotations at all. "
          "See extract_genes.py for a separate, non-tree approach to that.", flush=True)


if __name__ == "__main__":
    main()
