"""
enrich_mesh_subjects.py
------------------------
Fills in the `subjects` / `assigned_subjects1` gap in mesh_category_tree.json,
without re-running the full build_mesh_annotations.py pipeline.

Why the gap exists: fetch_pubmed_doc_info.py fetches PubTator3 annotations per
PMID, but PubTator3's NER pipeline runs on its own schedule and often hasn't
processed a paper yet by the time it lands in doc_all_info.csv — especially
recent (last few months) publications. build_mesh_annotations.py only resolves
whatever annotations happen to already be sitting in pubtator_records.jsonl.gz
at the time it's run, so newly-published docs come through with
docs[].subjects == "" until PubTator catches up and someone reruns the (much
more expensive, full-repo) build_mesh_annotations.py.

This script is the lightweight, incremental fix:

  1. Reads the *committed* data/mesh_category_tree.json.
  2. Finds every doc whose `subjects` field is still empty (or an explicit
     --pmids list).
  3. Re-fetches just those PMIDs from PubTator3 (through the same
     pubtator_records.jsonl.gz cache fetch_pubmed_doc_info.py uses, so a
     later full rebuild sees the same data).
  4. Resolves any Disease/Chemical MeSH IDs to their tree lineage and takes
     the *last* segment of each branch as the leaf term — e.g. "COVID-19"
     out of "Diseases.Infections....Pneumonia, Viral.COVID-19". This is
     exactly what makes the "search by last leaf, get matching documents"
     feature (see whn-analytics.net's category tree) work: docs[].subjects
     is a " | "-joined list of those leaf terms.
  5. Writes the results straight back into docs[].subjects /
     assigned_subjects1 and into tree[] (bumping counts / adding brand-new
     branches), and appends the same rows to mesh_annotations.csv so that
     file stays in sync too.

Resolving a MeSH ID to its tree lineage normally means downloading and
parsing NLM's full descriptor + supplementary-concept XML (what
build_mesh_annotations.py does — tens/hundreds of MB). Most MeSH IDs that
turn up in new PubTator annotations have already been seen before (COVID-19,
Long COVID, common comorbidities, ...), so this script builds its lineage
index for free straight out of the existing tree[] array. NLM's XML is only
downloaded (and cached, same as build_mesh_annotations.py) on the rarer
occasion a genuinely new MeSH ID shows up that isn't in tree[] yet.

Scope/caveat: by default this only ever touches docs with an EMPTY subjects
field, so tree[] counts are always purely additive — no double-counting risk.
Passing --pmids to explicitly re-annotate docs that already have subjects
breaks that guarantee (their old counts stay in tree[] and new ones are
added on top); if you need an exact recount after doing that, rerun
build_mesh_annotations.py from scratch.

Usage:
    python scripts/enrich_mesh_subjects.py
    python scripts/enrich_mesh_subjects.py --limit 200          # test run
    python scripts/enrich_mesh_subjects.py --pmids 42070008,42069416
    python scripts/enrich_mesh_subjects.py --force-refresh      # bypass the PubTator cache
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any, Iterator

# scripts/ is already on sys.path when this file is run directly, so this
# picks up the sibling module without needing the repo installed as a package.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_mesh_annotations import (  # noqa: E402
    MESH_BASE_URL,
    MESH_ID_PATTERN,
    MESH_RESOLVABLE_CATEGORIES,
    build_treenumber_index,
    build_treenumber_to_dui,
    clean_identifier,
    download,
    ensure_parent_dir,
    iter_annotations,
    parse_descriptors,
    parse_supplementary,
    resolve_mesh_id,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DATA_DIR = os.path.join(BASE_DIR, "data")
SCRATCH_DIR = os.path.join(BASE_DIR, ".mesh_cache")  # never committed — see .gitignore, shared with build_mesh_annotations.py

DEFAULT_TREE_JSON = os.path.join(DATA_DIR, "mesh_category_tree.json")
DEFAULT_ANNOTATIONS_CSV = os.path.join(DATA_DIR, "mesh_annotations.csv")
DEFAULT_PUBTATOR_CACHE = os.path.join(DATA_DIR, "pubtator_records.jsonl.gz")

PUBTATOR_URL = (
    "https://www.ncbi.nlm.nih.gov/research/pubtator-api/publications/export/biocjson"
    "?pmids={pmids}"
)

BATCH_SIZE = 20  # same conservative size fetch_pubmed_doc_info.py uses
MAX_RETRIES = 3
BACKOFF_BASE = 2.0

MAX_GIT_FILE_BYTES = 90 * 1024 * 1024  # stay under GitHub's hard 100MB push limit with headroom


def git_checkpoint(*paths: str, message: str) -> None:
    """Same safety net used by the other scripts in this pipeline: commits
    progress periodically so a killed/timed-out Action run doesn't lose
    everything, and never attempts to commit a file over the safe size
    threshold. Never raises."""
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


# ---------------------------------------------------------------------------
# PubTator fetch + cache (same shape as fetch_pubmed_doc_info.py, kept
# self-contained here so this script has no import dependency on it)
# ---------------------------------------------------------------------------

def parse_pubtator_response(raw: str) -> list[dict[str, Any]]:
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and "PubTator3" in parsed:
        return parsed["PubTator3"] or []
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        return [parsed]
    return []


def fetch_batch(pmids: list[str], api_key: str = "") -> tuple[list[dict[str, Any]], str]:
    url = PUBTATOR_URL.format(pmids=",".join(pmids))
    if api_key:
        url += "&api_key=" + api_key

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "enrich_mesh_subjects/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                raw = resp.read().decode("utf-8").strip()
            if not raw:
                if attempt < MAX_RETRIES:
                    time.sleep(BACKOFF_BASE**attempt)
                    continue
                return [], "no_record"
            return parse_pubtator_response(raw), ""
        except urllib.error.HTTPError as exc:
            reason = f"http_{exc.code}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE**attempt)
                continue
            return [], reason
        except TimeoutError:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE**attempt)
                continue
            return [], "timeout"
        except json.JSONDecodeError:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE**attempt)
                continue
            return [], "parse_error"
        except Exception as exc:
            if attempt < MAX_RETRIES:
                time.sleep(BACKOFF_BASE**attempt)
                continue
            return [], f"network_error:{exc}"
    return [], "network_error"


def load_pubtator_cache(path: str) -> dict[str, Any]:
    """{pmid: record}. Later lines win, mirroring fetch_pubmed_doc_info.py."""
    cache: dict[str, Any] = {}
    if not os.path.exists(path):
        return cache
    with gzip.open(path, "rt", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            pmid = str(entry.get("pmid", ""))
            if pmid:
                cache[pmid] = entry.get("record")
    return cache


def append_pubtator_cache(path: str, pmid: str, record: dict[str, Any]) -> None:
    """Real gzip append (concatenated gzip members) — no decompress/rewrite."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = (json.dumps({"pmid": pmid, "record": record}, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "ab") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            gz.write(line)


# ---------------------------------------------------------------------------
# Tree JSON I/O + the "resolve from what we already know" index
# ---------------------------------------------------------------------------

def load_tree_json(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def save_tree_json(path: str, data: dict[str, Any]) -> None:
    ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)


def build_mesh_index_from_tree(tree: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """{raw_mesh_id: [{"name", "path", "tree_id", "web_id"}, ...]} built for
    free out of the tree[] this repo already has committed — no NLM download
    needed for any MeSH ID that's shown up before."""
    index: dict[str, list[dict[str, str]]] = defaultdict(list)
    for entry in tree:
        mesh_id = entry.get("mesh_id", "")
        path = entry.get("first", "")
        if not mesh_id or not path:
            continue
        name = path.rsplit(".", 1)[-1]
        index[mesh_id].append({
            "name": name,
            "path": path,
            "tree_id": entry.get("tree_id", ""),
            "web_id": entry.get("web_id", ""),
        })
    return index


def leaf_name(path: str) -> str:
    return path.rsplit(".", 1)[-1] if path else ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree-json", default=DEFAULT_TREE_JSON)
    parser.add_argument("--annotations-csv", default=DEFAULT_ANNOTATIONS_CSV, help="Appended to, to stay in sync with tree-json")
    parser.add_argument("--pubtator-cache", default=DEFAULT_PUBTATOR_CACHE)
    parser.add_argument("--scratch-dir", default=SCRATCH_DIR, help="Where NLM descriptor/supp files get cached if a brand-new MeSH ID shows up")
    parser.add_argument("--year", default="2026", help="MeSH production year, only used for the brand-new-ID fallback")
    parser.add_argument("--pmids", default="", help="Comma-separated PMIDs to target explicitly, overriding the empty-subjects scan (see caveat in the module docstring)")
    parser.add_argument("--limit", type=int, help="Only process the first N target PMIDs, for testing")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay between PubTator batches")
    parser.add_argument("--api-key", default="", help="Optional NCBI/PubTator API key")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass the PubTator cache and re-fetch fresh annotations for the target PMIDs")
    parser.add_argument("--checkpoint-every", type=int, default=20, help="Commit progress every N batches (0 disables)")
    parser.add_argument("--no-checkpoint-commit", action="store_true", help="Write files to disk but skip git commit/push")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and resolve, but don't write tree-json/annotations-csv/cache")
    args = parser.parse_args()

    if not os.path.exists(args.tree_json):
        print(f"ERROR: {args.tree_json} not found. Run build_mesh_annotations.py at least once first.", flush=True)
        return

    tree_data = load_tree_json(args.tree_json)
    docs: list[dict[str, Any]] = tree_data.get("docs", [])
    tree: list[dict[str, Any]] = tree_data.get("tree", [])
    docs_by_pmid = {d["pmid"]: d for d in docs if d.get("pmid")}

    if args.pmids:
        target_pmids = [p.strip() for p in args.pmids.split(",") if p.strip()]
        target_pmids = [p for p in target_pmids if p in docs_by_pmid]
        print("--pmids given: recount caveat in the module docstring applies to any of these that already have subjects.", flush=True)
    else:
        target_pmids = [d["pmid"] for d in docs if d.get("pmid") and not d.get("subjects")]

    if args.limit:
        target_pmids = target_pmids[: args.limit]
    scope = "explicitly listed" if args.pmids else "empty subjects"
    print(f"{len(target_pmids)} PMIDs targeted ({scope})", flush=True)
    if not target_pmids:
        print("Nothing to do.", flush=True)
        return

    mesh_index = build_mesh_index_from_tree(tree)
    print(f"Lineage index built from tree.json: {len(mesh_index)} distinct MeSH IDs already known", flush=True)

    # tree_counts starts from what's already committed, so all updates are
    # additive on top of the real current state.
    tree_counts: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in tree:
        mesh_id, path = entry.get("mesh_id", ""), entry.get("first", "")
        if not mesh_id or not path:
            continue
        tree_counts[(mesh_id, path)] = {
            "count": int(entry.get("count(*)", 0) or 0),
            "web_id": entry.get("web_id", ""),
            "tree_id": entry.get("tree_id", ""),
        }

    pubtator_cache = {} if args.force_refresh else load_pubtator_cache(args.pubtator_cache)
    print(f"PubTator cache: {len(pubtator_cache)} records loaded", flush=True)

    # --- Fetch (or reuse cached) PubTator records for every target PMID ---
    records_by_pmid: dict[str, dict[str, Any]] = {}
    to_fetch = [p for p in target_pmids if p not in pubtator_cache]
    for p in target_pmids:
        if p in pubtator_cache:
            records_by_pmid[p] = pubtator_cache[p]

    print(f"PubTator: {len(target_pmids) - len(to_fetch)} already cached, {len(to_fetch)} to fetch", flush=True)
    still_missing: dict[str, str] = {}
    for start in range(0, len(to_fetch), args.batch_size):
        batch = to_fetch[start : start + args.batch_size]
        batch_num = start // args.batch_size + 1
        total_batches = (len(to_fetch) + args.batch_size - 1) // args.batch_size
        fetched, reason = fetch_batch(batch, args.api_key)
        fetched_by_pmid = {str(rec.get("pmid", rec.get("id", ""))): rec for rec in fetched if rec}
        for pmid in batch:
            record = fetched_by_pmid.get(pmid)
            if record:
                if not args.dry_run:
                    append_pubtator_cache(args.pubtator_cache, pmid, record)
                pubtator_cache[pmid] = record
                records_by_pmid[pmid] = record
            else:
                # Legitimately common: PubTator3 just hasn't annotated this
                # paper yet. Not cached, so a later run retries automatically.
                still_missing[pmid] = reason or "absent_from_response"
        print(f"PubTator batch {batch_num}/{total_batches} done ({len(fetched_by_pmid)}/{len(batch)} returned)", flush=True)
        if args.checkpoint_every and batch_num % args.checkpoint_every == 0 and not args.no_checkpoint_commit and not args.dry_run:
            git_checkpoint(args.pubtator_cache, message=f"Checkpoint: PubTator fetch for mesh-subjects enrichment, batch {batch_num}/{total_batches} [skip ci]")
        if args.sleep:
            time.sleep(args.sleep)

    if still_missing:
        print(f"{len(still_missing)} PMIDs still have no PubTator annotations available (will stay empty, retried next run)", flush=True)

    # --- Resolve annotations -> leaf subjects, tracking any brand-new MeSH IDs ---
    new_annotation_rows: list[dict[str, Any]] = []
    pending_unresolved: list[tuple[str, str, str, str]] = []  # (pmid, mesh_id, ann_id, key_text)
    subjects_by_pmid: dict[str, list[str]] = defaultdict(list)

    def apply_branches(pmid: str, ann: dict[str, Any], branches: list[dict[str, str]]) -> None:
        if not branches:
            new_annotation_rows.append({
                **ann, "file_name": f"{pmid}__.xml", "key_text1": "", "mesh_path": "",
                "web_id": "", "tree_id": "", "branch_index": 0,
            })
            return
        for i, branch in enumerate(branches):
            new_annotation_rows.append({
                **ann, "file_name": f"{pmid}__.xml", "key_text1": branch["name"], "mesh_path": branch["path"],
                "web_id": branch["web_id"], "tree_id": branch["tree_id"], "branch_index": i,
            })
            if branch["name"] and branch["name"] not in subjects_by_pmid[pmid]:
                subjects_by_pmid[pmid].append(branch["name"])
            if branch["path"]:
                key = (ann["mesh_id"], branch["path"])
                if key not in tree_counts:
                    tree_counts[key] = {"count": 0, "web_id": branch["web_id"], "tree_id": branch["tree_id"]}
                tree_counts[key]["count"] += 1

    for pmid, record in records_by_pmid.items():
        for ann in iter_annotations(record, pmid):
            if ann["key_name"] not in MESH_RESOLVABLE_CATEGORIES or not MESH_ID_PATTERN.match(ann["mesh_id"]):
                continue
            branches = mesh_index.get(ann["mesh_id"])
            if branches is not None:
                apply_branches(pmid, ann, branches)
            else:
                pending_unresolved.append((pmid, ann["mesh_id"], ann["ann_id"], ann["key_text"]))

    # --- Brand-new MeSH IDs: fall back to the full NLM descriptor/supp parse,
    # exactly like build_mesh_annotations.py, only for the IDs we actually need ---
    if pending_unresolved:
        unresolved_ids = sorted({mesh_id for _, mesh_id, _, _ in pending_unresolved})
        print(f"{len(unresolved_ids)} MeSH IDs not in the existing tree — downloading NLM MeSH files to resolve them", flush=True)
        desc_path = download(f"{MESH_BASE_URL}/desc{args.year}.gz", os.path.join(args.scratch_dir, f"desc{args.year}.gz"))
        supp_path = download(f"{MESH_BASE_URL}/supp{args.year}.gz", os.path.join(args.scratch_dir, f"supp{args.year}.gz"))
        descriptors = parse_descriptors(desc_path)
        supplements = parse_supplementary(supp_path)
        treenumber_index = build_treenumber_index(descriptors)
        treenumber_to_dui = build_treenumber_to_dui(descriptors)

        resolved_new: dict[str, list[dict[str, str]]] = {}
        for mesh_id in unresolved_ids:
            resolved_new[mesh_id] = resolve_mesh_id(mesh_id, descriptors, supplements, treenumber_index, treenumber_to_dui)
            mesh_index[mesh_id] = resolved_new[mesh_id]  # so a second brand-new ID for the same PMID also hits this

        for pmid, mesh_id, ann_id, key_text in pending_unresolved:
            ann = {"id": pmid, "mesh_id": mesh_id, "ann_id": ann_id, "key_category": "type", "key_name": "Disease", "key_text": key_text}
            apply_branches(pmid, ann, resolved_new[mesh_id])

    print(f"Resolved subjects for {len(subjects_by_pmid)} PMIDs", flush=True)

    # --- Write subjects/assigned_subjects1 back into docs[] ---
    updated = 0
    for pmid, names in subjects_by_pmid.items():
        doc = docs_by_pmid.get(pmid)
        if not doc:
            continue
        subjects = " | ".join(names)
        if subjects:
            subjects += " | "
        doc["subjects"] = subjects
        doc["assigned_subjects1"] = subjects
        updated += 1
    print(f"Updated docs[].subjects for {updated} documents", flush=True)

    # --- Rebuild tree[] from tree_counts (existing + newly bumped/added) ---
    new_tree = [
        {"mesh_id": mesh_id, "web_id": info["web_id"], "tree_id": info["tree_id"], "count(*)": str(info["count"]), "first": path}
        for (mesh_id, path), info in sorted(tree_counts.items(), key=lambda kv: -kv[1]["count"])
    ]
    tree_data["tree"] = new_tree
    tree_data["docs"] = docs
    # These parallel arrays are positional against docs[] elsewhere in this
    # pipeline (see build_mesh_annotations.py) — keep them in lockstep since
    # docs[] itself wasn't reordered, only mutated in place.
    tree_data["factors"] = [{"factor": d.get("factor", "")} for d in docs]
    tree_data["citations"] = [{"number_citations": d.get("number_citations", "")} for d in docs]
    tree_data["hard_category"] = [{"hard_category": d.get("hard_category", "")} for d in docs]
    tree_data["formats"] = [{"format": d.get("format", "")} for d in docs]

    if args.dry_run:
        print("--dry-run: not writing tree-json, annotations-csv, or committing.", flush=True)
        return

    save_tree_json(args.tree_json, tree_data)
    print(f"Wrote {len(new_tree)} tree entries / {len(docs)} docs to {args.tree_json}", flush=True)

    if new_annotation_rows:
        fieldnames = [
            "file_name", "id", "mesh_id", "ann_id", "key_category", "key_name", "key_text",
            "key_text1", "mesh_path", "web_id", "tree_id", "branch_index",
        ]
        file_exists = os.path.exists(args.annotations_csv)
        ensure_parent_dir(args.annotations_csv)
        with open(args.annotations_csv, "a", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerows(new_annotation_rows)
        print(f"Appended {len(new_annotation_rows)} rows to {args.annotations_csv}", flush=True)

    if not args.no_checkpoint_commit:
        git_checkpoint(
            args.tree_json, args.annotations_csv, args.pubtator_cache,
            message=f"Enrich mesh_category_tree.json subjects for {updated} PMIDs [skip ci]",
        )


if __name__ == "__main__":
    main()
