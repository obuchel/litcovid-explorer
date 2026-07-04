"""
build_mesh_annotations.py
--------------------------
Reads every PubTator3 annotation out of data/pubtator_records.jsonl, resolves
each Disease/Chemical mention's MeSH ID to its official name and full tree
lineage (e.g. "Diseases.Cardiovascular Diseases.Heart Diseases"), and writes:

  1. data/mesh_annotations.csv  — one row per (annotation x tree branch),
     same shape as the legacy all_subjects_enh.csv, with two new columns:
     mesh_path (dot-joined lineage) and branch_index (0, 1, 2... when a term
     sits in more than one place in the MeSH tree — all branches are kept,
     the row is repeated once per branch).

  2. data/mesh_category_tree.json — aggregated {mesh_id, count(*), first}
     records in the same shape as the whn-analytics.net category tree.

MeSH descriptor/supplementary-concept files (desc<year>.gz, supp<year>.gz)
are downloaded straight from NLM into a scratch directory and are NOT meant
to be committed to git — they're tens/hundreds of MB and NLM already hosts
them permanently. Only the small derived CSV/JSON outputs above get committed.

Usage:
    python scripts/build_mesh_annotations.py
    python scripts/build_mesh_annotations.py --year 2026 --limit 500
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any, Iterator

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DATA_DIR = os.path.join(BASE_DIR, "data")
SCRATCH_DIR = os.path.join(BASE_DIR, ".mesh_cache")  # never committed — see .gitignore

DEFAULT_JSONL = os.path.join(DATA_DIR, "pubtator_records.jsonl.gz")
DEFAULT_ANNOTATIONS_OUT = os.path.join(DATA_DIR, "mesh_annotations.csv")
DEFAULT_TREE_OUT = os.path.join(DATA_DIR, "mesh_category_tree.json")

MESH_BASE_URL = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh"

# Standard top-level MeSH tree categories. These single letters aren't
# descriptors themselves (they have no DescriptorUI), so they can't be
# resolved from the XML — they're NLM's fixed, published top-level scheme.
TOP_LEVEL_CATEGORIES = {
    "A": "Anatomy",
    "B": "Organisms",
    "C": "Diseases",
    "D": "Chemicals and Drugs",
    "E": "Analytical, Diagnostic and Therapeutic Techniques and Equipment",
    "F": "Psychiatry and Psychology",
    "G": "Phenomena and Processes",
    "H": "Disciplines and Occupations",
    "I": "Anthropology, Education, Sociology and Social Phenomena",
    "J": "Technology, Industry, Agriculture",
    "K": "Humanities",
    "L": "Information Science",
    "M": "Named Groups",
    "N": "Health Care",
    "V": "Publication Characteristics",
    "Z": "Geographicals",
}

# Only these annotation types get a MeSH ID worth resolving (Species use NCBI
# Taxonomy IDs, Genes use NCBI Gene/Homologene IDs — neither lives in MeSH).
MESH_RESOLVABLE_CATEGORIES = {"Disease", "Chemical"}
MESH_ID_PATTERN = re.compile(r"^[CD]\d{6,9}$")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def ensure_parent_dir(path: str) -> None:
    """os.makedirs(os.path.dirname(path)) blows up with FileNotFoundError when
    path has no directory component at all (e.g. a bare 'custom.csv') — dirname
    returns '' in that case, which isn't a valid path to create."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


def download(url: str, dest_path: str) -> str:
    if os.path.exists(dest_path):
        print(f"Using cached download: {dest_path}")
        return dest_path
    ensure_parent_dir(dest_path)
    print(f"Downloading {url} ...")
    urllib.request.urlretrieve(url, dest_path)
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"Saved {dest_path} ({size_mb:.1f} MB)")
    return dest_path


# ---------------------------------------------------------------------------
# MeSH descriptor (desc<year>.gz) parsing
# ---------------------------------------------------------------------------

def iter_xml_records(gz_path: str, record_tag: str) -> Iterator[ET.Element]:
    """Stream-parse a gzipped MeSH XML file without loading it all into memory."""
    with gzip.open(gz_path, "rb") as fp:
        context = ET.iterparse(fp, events=("end",))
        for _, elem in context:
            if elem.tag == record_tag:
                yield elem
                elem.clear()


def parse_descriptors(desc_gz_path: str) -> dict[str, dict[str, Any]]:
    """Returns {DUI: {"name": str, "tree_numbers": [str, ...]}}."""
    descriptors: dict[str, dict[str, Any]] = {}
    for record in iter_xml_records(desc_gz_path, "DescriptorRecord"):
        dui_el = record.find("DescriptorUI")
        name_el = record.find("DescriptorName/String")
        if dui_el is None or name_el is None:
            continue
        tree_numbers = [tn.text for tn in record.findall("TreeNumberList/TreeNumber") if tn.text]
        descriptors[dui_el.text] = {"name": name_el.text, "tree_numbers": tree_numbers}
    print(f"Parsed {len(descriptors)} MeSH descriptors")
    return descriptors


def parse_supplementary(supp_gz_path: str) -> dict[str, dict[str, Any]]:
    """Returns {SCR_UI: {"name": str, "mapped_dui": [DUI, ...]}}.

    Supplementary Concept Records (chemicals, rare diseases, etc.) don't have
    their own tree numbers — they're mapped to one or more MeSH descriptors
    via HeadingMappedTo, and we borrow that descriptor's tree position(s)."""
    records: dict[str, dict[str, Any]] = {}
    for record in iter_xml_records(supp_gz_path, "SupplementalRecord"):
        ui_el = record.find("SupplementalRecordUI")
        name_el = record.find("SupplementalRecordName/String")
        if ui_el is None or name_el is None:
            continue
        mapped_dui = [
            dui.text
            for dui in record.findall("HeadingMappedToList/HeadingMappedTo/DescriptorReferredTo/DescriptorUI")
            if dui.text
        ]
        records[ui_el.text] = {"name": name_el.text, "mapped_dui": mapped_dui}
    print(f"Parsed {len(records)} MeSH supplementary concept records")
    return records


def build_treenumber_index(descriptors: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Flatten every descriptor's tree numbers into {tree_number: name}, so any
    ancestor prefix (e.g. "C14" inside "C14.280.400") can be named too."""
    index: dict[str, str] = {}
    for info in descriptors.values():
        for tn in info["tree_numbers"]:
            index[tn] = info["name"]
    return index


def build_treenumber_to_dui(descriptors: dict[str, dict[str, Any]]) -> dict[str, str]:
    """{tree_number: DUI} — each tree number belongs to exactly one descriptor,
    so unlike inverting a name/DUI dict there's no collision here. This is what
    lets every branch get its own correct tree_id, instead of the old
    inv_trees = {v: k for k, v in trees.items()} approach silently keeping only
    the last tree number seen for a DUI that occupies more than one spot."""
    index: dict[str, str] = {}
    for dui, info in descriptors.items():
        for tn in info["tree_numbers"]:
            index[tn] = dui
    return index


def resolve_lineage(tree_number: str, treenumber_index: dict[str, str]) -> list[str]:
    """"C14.280.400" -> ["Diseases", "Cardiovascular Diseases", ...]."""
    segments = tree_number.split(".")
    path: list[str] = []
    top_letter = segments[0][0]
    if top_letter in TOP_LEVEL_CATEGORIES:
        path.append(TOP_LEVEL_CATEGORIES[top_letter])
    prefix = ""
    for i, seg in enumerate(segments):
        prefix = seg if i == 0 else f"{prefix}.{seg}"
        name = treenumber_index.get(prefix)
        if name and (not path or path[-1] != name):
            path.append(name)
    return path


def resolve_mesh_id(
    mesh_id: str,
    descriptors: dict[str, dict[str, Any]],
    supplements: dict[str, dict[str, Any]],
    treenumber_index: dict[str, str],
    treenumber_to_dui: dict[str, str],
) -> list[dict[str, str]]:
    """Returns one dict per branch the term occupies in the tree:
        {"name": display name, "path": dot-joined lineage,
         "tree_id": this branch's own tree number, "web_id": its real MeSH DUI}
    Empty list if it can't be resolved (e.g. an OMIM ID, or a descriptor with
    no TreeNumberList).

    For Supplementary Concept Records, web_id is the *mapped descriptor's*
    DUI (keeping it in the real-DUI space old tooling expects) while `name`
    stays the SCR's own name — the original SCR id is still preserved
    upstream as the row's `mesh_id`, so nothing is lost either way."""
    if mesh_id in descriptors:
        info = descriptors[mesh_id]
        branches = []
        for tn in info["tree_numbers"]:
            lineage = resolve_lineage(tn, treenumber_index)
            if lineage:
                branches.append({"name": info["name"], "path": ".".join(lineage), "tree_id": tn, "web_id": mesh_id})
        return branches or [{"name": info["name"], "path": "", "tree_id": "", "web_id": mesh_id}]

    if mesh_id in supplements:
        info = supplements[mesh_id]
        branches = []
        for dui in info["mapped_dui"]:
            mapped = descriptors.get(dui)
            if not mapped:
                continue
            for tn in mapped["tree_numbers"]:
                lineage = resolve_lineage(tn, treenumber_index)
                if lineage:
                    branches.append({"name": info["name"], "path": ".".join(lineage), "tree_id": tn, "web_id": dui})
        return branches or [{"name": info["name"], "path": "", "tree_id": "", "web_id": ""}]

    return []


# ---------------------------------------------------------------------------
# PubTator annotation extraction
# ---------------------------------------------------------------------------

def clean_identifier(raw: str) -> str:
    """PubTator3 identifiers are sometimes prefixed, e.g. "MESH:D012817"."""
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    return raw.strip()


def iter_annotations(record: dict[str, Any], pmid: str) -> Iterator[dict[str, Any]]:
    for passage in record.get("passages", []) or []:
        for ann in passage.get("annotations", []) or []:
            infons = ann.get("infons", {}) or {}
            identifier = infons.get("identifier") or infons.get("Identifier") or ""
            category = infons.get("type") or infons.get("Type") or ""
            yield {
                "id": pmid,
                "mesh_id": clean_identifier(str(identifier)) if identifier else "",
                "ann_id": ann.get("id", ""),
                "key_category": "type",
                "key_name": category,
                "key_text": ann.get("text", ""),
            }


def load_jsonl(path: str) -> Iterator[tuple[str, dict[str, Any]]]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            pmid = str(entry.get("pmid", ""))
            record = entry.get("record") or {}
            if pmid and record:
                yield pmid, record


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jsonl", default=DEFAULT_JSONL, help="Path to pubtator_records.jsonl.gz")
    parser.add_argument("--year", default="2026", help="MeSH production year (matches NLM's file naming)")
    parser.add_argument("--scratch-dir", default=SCRATCH_DIR, help="Where to download MeSH files (not committed)")
    parser.add_argument("--out-annotations", default=DEFAULT_ANNOTATIONS_OUT)
    parser.add_argument("--out-tree", default=DEFAULT_TREE_OUT)
    parser.add_argument(
        "--only-categories",
        default="Disease,Chemical",
        help="Comma-separated key_name values to keep in the output (empty = keep everything)",
    )
    parser.add_argument("--limit", type=int, help="Only process the first N PMIDs, for testing")
    args = parser.parse_args()

    desc_path = download(f"{MESH_BASE_URL}/desc{args.year}.gz", os.path.join(args.scratch_dir, f"desc{args.year}.gz"))
    supp_path = download(f"{MESH_BASE_URL}/supp{args.year}.gz", os.path.join(args.scratch_dir, f"supp{args.year}.gz"))

    descriptors = parse_descriptors(desc_path)
    supplements = parse_supplementary(supp_path)
    treenumber_index = build_treenumber_index(descriptors)
    treenumber_to_dui = build_treenumber_to_dui(descriptors)

    only_categories = {c.strip() for c in args.only_categories.split(",") if c.strip()} or None

    rows: list[dict[str, Any]] = []
    tree_counts: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0, "first": None, "web_id": None, "tree_id": None})

    resolved_cache: dict[str, list[dict[str, str]]] = {}
    pmid_count = 0

    for pmid, record in load_jsonl(args.jsonl):
        if args.limit and pmid_count >= args.limit:
            break
        pmid_count += 1

        for ann in iter_annotations(record, pmid):
            if only_categories and ann["key_name"] not in only_categories:
                continue

            branches: list[dict[str, str]] = []
            if ann["key_name"] in MESH_RESOLVABLE_CATEGORIES and MESH_ID_PATTERN.match(ann["mesh_id"]):
                if ann["mesh_id"] not in resolved_cache:
                    resolved_cache[ann["mesh_id"]] = resolve_mesh_id(
                        ann["mesh_id"], descriptors, supplements, treenumber_index, treenumber_to_dui
                    )
                branches = resolved_cache[ann["mesh_id"]]

            if not branches:
                rows.append({
                    **ann, "file_name": f"{pmid}__.xml", "key_text1": "", "mesh_path": "",
                    "web_id": "", "tree_id": "", "branch_index": 0,
                })
                continue

            for i, branch in enumerate(branches):
                rows.append({
                    **ann, "file_name": f"{pmid}__.xml", "key_text1": branch["name"], "mesh_path": branch["path"],
                    "web_id": branch["web_id"], "tree_id": branch["tree_id"], "branch_index": i,
                })
                if branch["path"]:
                    key = (ann["mesh_id"], branch["path"])
                    tree_counts[key]["count"] += 1
                    tree_counts[key]["web_id"] = branch["web_id"]
                    tree_counts[key]["tree_id"] = branch["tree_id"]
                    if tree_counts[key]["first"] is None:
                        tree_counts[key]["first"] = branch["path"]

    ensure_parent_dir(args.out_annotations)
    fieldnames = [
        "file_name", "id", "mesh_id", "ann_id", "key_category", "key_name", "key_text",
        "key_text1", "mesh_path", "web_id", "tree_id", "branch_index",
    ]
    import csv as csv_module

    with open(args.out_annotations, "w", encoding="utf-8", newline="") as fp:
        writer = csv_module.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} annotation rows to {args.out_annotations}")

    tree = [
        {"mesh_id": mesh_id, "web_id": info["web_id"], "tree_id": info["tree_id"], "count(*)": info["count"], "first": info["first"]}
        for (mesh_id, _path), info in sorted(tree_counts.items(), key=lambda kv: -kv[1]["count"])
    ]
    ensure_parent_dir(args.out_tree)
    with open(args.out_tree, "w", encoding="utf-8") as fp:
        json.dump({"tree": tree}, fp, ensure_ascii=False, indent=2)
    print(f"Wrote {len(tree)} tree entries to {args.out_tree}")


if __name__ == "__main__":
    main()
