"""
extract_genes.py
------------------
Companion to build_mesh_annotations.py. That script explicitly discards
Gene-type PubTator annotations (MESH_RESOLVABLE_CATEGORIES = {"Disease",
"Chemical"}), because genes are identified by NCBI Entrez Gene ID, a
namespace MeSH doesn't cover at all. This script reads the same
pubtator_records.jsonl.gz cache, pulls out only the Gene-type annotations,
and builds a "mesh_category_tree.json"-shaped {tree, docs} file for genes —
using four different external classifications to supply the hierarchy that
Entrez IDs themselves don't have:

  1. --approach go     Gene Ontology (gene2go.gz + go-basic.obo). Closest
                        structural match to the MeSH tree: three root
                        namespaces (Biological Process / Molecular Function /
                        Cellular Component), deep is_a hierarchy under each.
  2. --approach hgnc    HGNC gene groups (hgnc_complete_set.txt). Shallower,
                        human-only, but biologically intuitive family names
                        (e.g. "Protein kinases"). Two-level lineage:
                        locus_group -> gene_group.
  3. --approach kegg    KEGG BRITE pathway hierarchy (br:br08901) + KEGG gene
                        -> pathway links. Functional/pathway context rather
                        than molecular annotation. Requires network access to
                        rest.kegg.jp at run time (not bundled/cached like the
                        NLM/NCBI files, and KEGG's redistribution terms are
                        more restrictive, so nothing from KEGG is meant to be
                        committed to git beyond the small derived tree/docs
                        file).
  4. --approach type    NCBI gene_info.gz type_of_gene field. Not really a
                        tree (one flat level: protein-coding, ncRNA,
                        pseudogene, ...) but near-zero effort and always
                        available.

The default (--approach go,hgnc,kegg,type) builds all four as separate
output files, so each can be browsed/compared independently — mirroring how
split_category_trees.py produces separate per-category files instead of one
combined one.

Usage:
    python scripts/extract_genes.py
    python scripts/extract_genes.py --approach go,type --tax-id 9606
    python scripts/extract_genes.py --approach kegg --limit 500
"""

from __future__ import annotations

import argparse
import csv as csv_module
import gzip
import json
import os
import re
import subprocess
import urllib.request
from collections import defaultdict
from typing import Any, Iterator

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DATA_DIR = os.path.join(BASE_DIR, "data")
SCRATCH_DIR = os.path.join(BASE_DIR, ".mesh_cache")  # shared with build_mesh_annotations.py, gitignored

DEFAULT_JSONL = os.path.join(DATA_DIR, "pubtator_records.jsonl.gz")
DEFAULT_DOC_INFO_CSV = os.path.join(DATA_DIR, "doc_all_info.csv")
DEFAULT_OUT_PREFIX = os.path.join(DATA_DIR, "gene_category_tree")

NCBI_GENE_BASE = "https://ftp.ncbi.nlm.nih.gov/gene/DATA"
GO_OBO_URL = "http://purl.obolibrary.org/obo/go/go-basic.obo"
KEGG_REST_BASE = "https://rest.kegg.jp"
HGNC_TSV_URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"

MAX_GIT_FILE_BYTES = 90 * 1024 * 1024  # stay under GitHub's hard 100MB push limit with headroom

GO_NAMESPACE_LABELS = {
    "biological_process": "Biological Process",
    "molecular_function": "Molecular Function",
    "cellular_component": "Cellular Component",
}


# ---------------------------------------------------------------------------
# Shared helpers (same behavior as build_mesh_annotations.py / split_category_trees.py)
# ---------------------------------------------------------------------------

def ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)


USER_AGENT = "Mozilla/5.0 (compatible; litcovid-explorer-extract-genes/1.0)"


def download(url: str, dest_path: str) -> str:
    if os.path.exists(dest_path):
        print(f"Using cached download: {dest_path}")
        return dest_path
    ensure_parent_dir(dest_path)
    print(f"Downloading {url} ...")
    # Plain urlretrieve sends urllib's default User-Agent, which some hosts
    # (e.g. purl.obolibrary.org, which redirects to geneontology.org) 403
    # outright. A browser-like UA avoids that without changing anything else.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as response, open(dest_path, "wb") as out_file:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out_file.write(chunk)
    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"Saved {dest_path} ({size_mb:.1f} MB)")
    return dest_path


def git_checkpoint(*paths: str, message: str) -> None:
    """Best-effort `git add` + `git commit` for the given output paths. Never
    raises — a failed checkpoint shouldn't take down the extraction run. Any
    path over MAX_GIT_FILE_BYTES is skipped (left on disk, just not staged),
    same guard build_mesh_annotations.py's pipeline uses for mesh_annotations.csv."""
    stageable = []
    for path in paths:
        if not os.path.exists(path):
            continue
        if os.path.getsize(path) > MAX_GIT_FILE_BYTES:
            print(f"Skipping git add for {path}: over {MAX_GIT_FILE_BYTES / (1024*1024):.0f}MB")
            continue
        stageable.append(path)
    if not stageable:
        return
    try:
        subprocess.run(["git", "add", *stageable], cwd=BASE_DIR, check=True, capture_output=True)
        result = subprocess.run(
            ["git", "commit", "-m", message], cwd=BASE_DIR, capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"git checkpoint: {message}")
        else:
            # Most common non-error case: nothing changed since last checkpoint.
            print(f"git checkpoint skipped (nothing to commit): {message}")
    except Exception as exc:  # noqa: BLE001 - best-effort, never fatal
        print(f"git checkpoint failed (continuing anyway): {exc}")


# ---------------------------------------------------------------------------
# PubTator: pull out Gene-type annotations only
# ---------------------------------------------------------------------------

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


def clean_gene_ids(raw: str) -> list[str]:
    """PubTator3 sometimes packs more than one Entrez Gene ID into a single
    annotation's identifier field (e.g. gene families/complexes), separated
    by ';' — occasionally ',' turns up too. Also strips a "Gene:" prefix if
    present, matching build_mesh_annotations.py's clean_identifier."""
    if not raw:
        return []
    if ":" in raw and not raw[0].isdigit():
        raw = raw.split(":", 1)[1]
    parts = re.split(r"[;,]", raw)
    return [p.strip() for p in parts if p.strip() and p.strip().lower() != "none"]


def iter_gene_annotations(record: dict[str, Any], pmid: str) -> Iterator[dict[str, Any]]:
    for passage in record.get("passages", []) or []:
        for ann in passage.get("annotations", []) or []:
            infons = ann.get("infons", {}) or {}
            category = infons.get("type") or infons.get("Type") or ""
            if category != "Gene":
                continue
            identifier = infons.get("identifier") or infons.get("Identifier") or ""
            for gene_id in clean_gene_ids(str(identifier)):
                yield {
                    "id": pmid,
                    "gene_id": gene_id,
                    "ann_id": ann.get("id", ""),
                    "key_text": ann.get("text", ""),
                }


def collect_gene_mentions(jsonl_path: str, limit: int | None) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
    """One pass over the jsonl, shared by every approach so we don't re-read
    a potentially multi-GB file four times. Returns (rows, gene_ids_by_pmid)."""
    rows: list[dict[str, Any]] = []
    gene_ids_by_pmid: dict[str, set[str]] = defaultdict(set)
    pmid_count = 0
    for pmid, record in load_jsonl(jsonl_path):
        if limit and pmid_count >= limit:
            break
        pmid_count += 1
        for ann in iter_gene_annotations(record, pmid):
            rows.append(ann)
            gene_ids_by_pmid[pmid].add(ann["gene_id"])
    print(f"Found {len(rows)} Gene-type annotation mentions across {pmid_count} PMIDs "
          f"({sum(len(v) for v in gene_ids_by_pmid.values())} gene-id x pmid pairs, "
          f"{len({g for gs in gene_ids_by_pmid.values() for g in gs})} distinct gene IDs)")
    return rows, gene_ids_by_pmid


# ---------------------------------------------------------------------------
# Approach 1: Gene Ontology (gene2go.gz + go-basic.obo)
# ---------------------------------------------------------------------------

def parse_go_obo(obo_path: str) -> dict[str, dict[str, Any]]:
    """Returns {go_id: {"name": str, "namespace": str, "is_a": [go_id, ...]}}.
    OBO is a simple stanza format — no need for a full parser library."""
    terms: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    with open(obo_path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.rstrip("\n")
            if line == "[Term]":
                current = {"is_a": []}
                continue
            if line.startswith("[") and line.endswith("]"):
                current = None  # Typedef or other stanza — not a GO term
                continue
            if current is None:
                continue
            if line.startswith("id: "):
                current["id"] = line[4:].strip()
            elif line.startswith("name: "):
                current["name"] = line[6:].strip()
            elif line.startswith("namespace: "):
                current["namespace"] = line[11:].strip()
            elif line.startswith("is_a: "):
                # "is_a: GO:0008150 ! biological_process" -> just the id
                parent = line[6:].split("!")[0].strip()
                current["is_a"].append(parent)
            elif line.startswith("is_obsolete: true"):
                current["obsolete"] = True
            elif line == "" and current is not None and "id" in current:
                if not current.get("obsolete"):
                    terms[current["id"]] = {
                        "name": current.get("name", current["id"]),
                        "namespace": current.get("namespace", ""),
                        "is_a": current["is_a"],
                    }
                current = None
        # File may not end with a trailing blank line after the last stanza
        if current is not None and "id" in current and not current.get("obsolete"):
            terms[current["id"]] = {
                "name": current.get("name", current["id"]),
                "namespace": current.get("namespace", ""),
                "is_a": current["is_a"],
            }
    print(f"Parsed {len(terms)} GO terms from {obo_path}")
    return terms


def go_lineage(go_id: str, terms: dict[str, dict[str, Any]], max_depth: int = 20) -> list[str]:
    """Walk is_a edges up to the namespace root, following the *first* parent
    at each step when a term has multiple is_a edges (GO is a DAG, not a
    strict tree, so a single canonical path is a simplification — same
    tradeoff build_mesh_annotations.py's resolve_lineage makes implicitly by
    just using the tree numbers NLM already assigned)."""
    info = terms.get(go_id)
    if not info:
        return []
    chain = [info["name"]]
    seen = {go_id}
    current = go_id
    for _ in range(max_depth):
        parents = terms.get(current, {}).get("is_a", [])
        if not parents:
            break
        nxt = parents[0]
        if nxt in seen or nxt not in terms:
            break
        seen.add(nxt)
        chain.append(terms[nxt]["name"])
        current = nxt
    root_label = GO_NAMESPACE_LABELS.get(info.get("namespace", ""), "Gene Ontology")
    return [root_label] + list(reversed(chain))


def parse_gene2go(gene2go_path: str, tax_id: str) -> dict[str, list[str]]:
    """Returns {entrez_gene_id: [go_id, ...]}, filtered to one tax_id (NCBI's
    gene2go.gz covers every organism NCBI tracks — human alone is a small
    fraction of the file, so filtering while streaming keeps memory sane)."""
    opener = gzip.open if gene2go_path.endswith(".gz") else open
    index: dict[str, list[str]] = defaultdict(list)
    with opener(gene2go_path, "rt", encoding="utf-8") as fp:
        header = fp.readline()  # "#tax_id GeneID GO_ID Evidence Qualifier GO_term PubMed Category"
        for line in fp:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            row_tax, gene_id, go_id = parts[0].lstrip("#"), parts[1], parts[2]
            if row_tax != tax_id:
                continue
            index[gene_id].append(go_id)
    print(f"Loaded GO annotations for {len(index)} genes (tax_id={tax_id}) from {gene2go_path}")
    return index


def build_tree_go(
    gene_ids_by_pmid: dict[str, set[str]],
    docs_meta: dict[str, dict[str, str]],
    scratch_dir: str,
    tax_id: str,
) -> dict[str, Any]:
    gene2go_path = download(f"{NCBI_GENE_BASE}/gene2go.gz", os.path.join(scratch_dir, "gene2go.gz"))
    obo_path = download(GO_OBO_URL, os.path.join(scratch_dir, "go-basic.obo"))
    gene2go = parse_gene2go(gene2go_path, tax_id)
    go_terms = parse_go_obo(obo_path)

    lineage_cache: dict[str, list[str]] = {}
    tree_counts: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0, "first": None, "web_id": None, "tree_id": None})
    names_by_pmid: dict[str, list[str]] = defaultdict(list)

    for pmid, gene_ids in gene_ids_by_pmid.items():
        for gene_id in gene_ids:
            for go_id in gene2go.get(gene_id, []):
                if go_id not in lineage_cache:
                    lineage_cache[go_id] = go_lineage(go_id, go_terms)
                lineage = lineage_cache[go_id]
                if not lineage:
                    continue
                path = ".".join(lineage)
                key = (gene_id, path)
                tree_counts[key]["count"] += 1
                tree_counts[key]["web_id"] = go_id
                tree_counts[key]["tree_id"] = go_id
                if tree_counts[key]["first"] is None:
                    tree_counts[key]["first"] = path
                if lineage[-1] not in names_by_pmid[pmid]:
                    names_by_pmid[pmid].append(lineage[-1])

    return assemble_output(tree_counts, names_by_pmid, docs_meta, gene_ids_by_pmid, id_label="gene_id")


# ---------------------------------------------------------------------------
# Approach 2: HGNC gene groups (locus_group -> gene_group, 2-level lineage)
# ---------------------------------------------------------------------------

def parse_hgnc(hgnc_path: str) -> dict[str, list[str]]:
    """Returns {entrez_gene_id: [dot-joined "locus_group.gene_group" path, ...]}.
    A gene can belong to more than one HGNC family (gene_group is itself
    pipe-separated), so — same as a MeSH term sitting in more than one tree
    branch — every group the gene belongs to becomes its own branch."""
    index: dict[str, list[str]] = defaultdict(list)
    with open(hgnc_path, "r", encoding="utf-8", newline="") as fp:
        reader = csv_module.DictReader(fp, delimiter="\t")
        for row in reader:
            entrez_id = (row.get("entrez_id") or "").strip()
            if not entrez_id:
                continue
            locus_group = (row.get("locus_group") or "Unclassified").strip() or "Unclassified"
            groups_raw = (row.get("gene_group") or "").strip()
            groups = [g.strip() for g in groups_raw.split("|") if g.strip()]
            if not groups:
                index[entrez_id].append(locus_group)  # still bucket by locus_group alone
            for group in groups:
                index[entrez_id].append(f"{locus_group}.{group}")
    print(f"Loaded HGNC group membership for {len(index)} genes from {hgnc_path}")
    return index


def build_tree_hgnc(
    gene_ids_by_pmid: dict[str, set[str]],
    docs_meta: dict[str, dict[str, str]],
    scratch_dir: str,
) -> dict[str, Any]:
    hgnc_path = download(HGNC_TSV_URL, os.path.join(scratch_dir, "hgnc_complete_set.txt"))
    hgnc_groups = parse_hgnc(hgnc_path)

    tree_counts: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0, "first": None, "web_id": None, "tree_id": None})
    names_by_pmid: dict[str, list[str]] = defaultdict(list)

    for pmid, gene_ids in gene_ids_by_pmid.items():
        for gene_id in gene_ids:
            for path in hgnc_groups.get(gene_id, []):
                key = (gene_id, path)
                tree_counts[key]["count"] += 1
                tree_counts[key]["web_id"] = gene_id
                tree_counts[key]["tree_id"] = path
                if tree_counts[key]["first"] is None:
                    tree_counts[key]["first"] = path
                leaf = path.split(".")[-1]
                if leaf not in names_by_pmid[pmid]:
                    names_by_pmid[pmid].append(leaf)

    return assemble_output(tree_counts, names_by_pmid, docs_meta, gene_ids_by_pmid, id_label="gene_id")


# ---------------------------------------------------------------------------
# Approach 3: KEGG BRITE pathway hierarchy + gene->pathway links
# ---------------------------------------------------------------------------

def fetch_kegg_text(path: str, dest_path: str) -> str:
    """KEGG's REST API (rest.kegg.jp) doesn't like being treated like a
    static file host — always hit it live rather than caching indefinitely,
    but still write to scratch_dir so a single run doesn't refetch the huge
    gene->pathway link table more than once."""
    if os.path.exists(dest_path):
        print(f"Using cached KEGG fetch: {dest_path}")
        with open(dest_path, "r", encoding="utf-8") as fp:
            return fp.read()
    url = f"{KEGG_REST_BASE}/{path}"
    print(f"Fetching {url} ...")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request) as resp:
        text = resp.read().decode("utf-8")
    ensure_parent_dir(dest_path)
    with open(dest_path, "w", encoding="utf-8") as fp:
        fp.write(text)
    return text


def parse_kegg_brite_pathway(brite_text: str) -> dict[str, list[str]]:
    """KEGG's br:br08901 ("KEGG pathway maps") htext format uses one leading
    letter per depth (A top category, B sub-category, C the pathway itself:
    "C    00010  Glycolysis / Gluconeogenesis"). Returns
    {generic_pathway_number (e.g. "00010"): [A_label, B_label, pathway_name]}."""
    index: dict[str, list[str]] = {}
    stack: dict[str, str] = {}
    for line in brite_text.splitlines():
        if not line or line[0] not in "ABCD":
            continue
        depth = line[0]
        rest = line[1:].strip()
        if not rest:
            continue
        rest = re.sub(r"</?b>", "", rest).strip()  # KEGG's htext bolds top-level category names
        if depth == "A":
            stack = {"A": rest}
        elif depth == "B":
            stack = {"A": stack.get("A", ""), "B": rest}
        elif depth == "C":
            # "00010  Glycolysis / Gluconeogenesis" or with trailing [PATH:...]
            m = re.match(r"(\d{5})\s+(.+)", rest)
            if not m:
                continue
            pathway_num, name = m.group(1), re.sub(r"\s*\[.*\]\s*$", "", m.group(2)).strip()
            path = [p for p in (stack.get("A"), stack.get("B"), name) if p]
            index[pathway_num] = path
    print(f"Parsed {len(index)} KEGG pathway entries from BRITE hierarchy")
    return index


def parse_kegg_gene_pathway_links(link_text: str, organism_prefix: str) -> dict[str, list[str]]:
    """Parses `link/pathway/<organism>` output: "hsa:672\tpath:hsa04110" per
    line. Returns {entrez_gene_id: [generic_pathway_number, ...]} — strips
    the organism prefix off both gene and pathway IDs so they join against
    parse_kegg_brite_pathway's generic (organism-agnostic) numbering."""
    index: dict[str, list[str]] = defaultdict(list)
    prefix_len = len(organism_prefix) + 1  # "hsa:"
    for line in link_text.splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        gene_col, path_col = line.split("\t", 1)
        gene_id = gene_col[prefix_len:] if gene_col.startswith(f"{organism_prefix}:") else gene_col
        pathway_id = path_col.replace(f"path:{organism_prefix}", "")
        if gene_id and pathway_id:
            index[gene_id].append(pathway_id)
    print(f"Loaded KEGG pathway links for {len(index)} genes")
    return index


def build_tree_kegg(
    gene_ids_by_pmid: dict[str, set[str]],
    docs_meta: dict[str, dict[str, str]],
    scratch_dir: str,
    kegg_organism: str,
) -> dict[str, Any]:
    brite_text = fetch_kegg_text("get/br:br08901", os.path.join(scratch_dir, "kegg_br08901.txt"))
    pathway_lineage = parse_kegg_brite_pathway(brite_text)
    link_text = fetch_kegg_text(f"link/pathway/{kegg_organism}", os.path.join(scratch_dir, f"kegg_gene_pathway_{kegg_organism}.txt"))
    gene_pathways = parse_kegg_gene_pathway_links(link_text, kegg_organism)

    tree_counts: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0, "first": None, "web_id": None, "tree_id": None})
    names_by_pmid: dict[str, list[str]] = defaultdict(list)

    for pmid, gene_ids in gene_ids_by_pmid.items():
        for gene_id in gene_ids:
            for pathway_num in gene_pathways.get(gene_id, []):
                lineage = pathway_lineage.get(pathway_num)
                if not lineage:
                    continue
                path = ".".join(lineage)
                key = (gene_id, path)
                tree_counts[key]["count"] += 1
                tree_counts[key]["web_id"] = f"{kegg_organism}{pathway_num}"
                tree_counts[key]["tree_id"] = pathway_num
                if tree_counts[key]["first"] is None:
                    tree_counts[key]["first"] = path
                if lineage[-1] not in names_by_pmid[pmid]:
                    names_by_pmid[pmid].append(lineage[-1])

    return assemble_output(tree_counts, names_by_pmid, docs_meta, gene_ids_by_pmid, id_label="gene_id")


# ---------------------------------------------------------------------------
# Approach 4: NCBI gene_info.gz type_of_gene (flat, one level)
# ---------------------------------------------------------------------------

def parse_gene_info_type(gene_info_path: str, tax_id: str) -> dict[str, str]:
    """Returns {entrez_gene_id: type_of_gene}. gene_info.gz columns:
    tax_id GeneID Symbol LocusTag Synonyms dbXrefs chromosome map_location
    description type_of_gene ..."""
    opener = gzip.open if gene_info_path.endswith(".gz") else open
    index: dict[str, str] = {}
    with opener(gene_info_path, "rt", encoding="utf-8") as fp:
        header_line = fp.readline().lstrip("#").rstrip("\n")
        columns = header_line.split("\t")
        try:
            tax_idx, gene_idx, type_idx = columns.index("tax_id"), columns.index("GeneID"), columns.index("type_of_gene")
        except ValueError:
            # Fallback to the fixed positions NCBI has used for this file for years
            tax_idx, gene_idx, type_idx = 0, 1, 9
        for line in fp:
            parts = line.rstrip("\n").split("\t")
            if len(parts) <= max(tax_idx, gene_idx, type_idx):
                continue
            if parts[tax_idx] != tax_id:
                continue
            index[parts[gene_idx]] = parts[type_idx]
    print(f"Loaded type_of_gene for {len(index)} genes (tax_id={tax_id}) from {gene_info_path}")
    return index


def build_tree_type(
    gene_ids_by_pmid: dict[str, set[str]],
    docs_meta: dict[str, dict[str, str]],
    scratch_dir: str,
    tax_id: str,
) -> dict[str, Any]:
    # Human gets NCBI's small per-organism file; anything else falls back to
    # the full multi-organism gene_info.gz (several GB) filtered by tax_id below.
    if tax_id == "9606":
        gene_info_url = f"{NCBI_GENE_BASE}/GENE_INFO/Mammalia/Homo_sapiens.gene_info.gz"
    else:
        gene_info_url = f"{NCBI_GENE_BASE}/gene_info.gz"
    gene_info_path = download(gene_info_url, os.path.join(scratch_dir, f"gene_info_{tax_id}.gz"))
    gene_types = parse_gene_info_type(gene_info_path, tax_id)

    tree_counts: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"count": 0, "first": None, "web_id": None, "tree_id": None})
    names_by_pmid: dict[str, list[str]] = defaultdict(list)

    for pmid, gene_ids in gene_ids_by_pmid.items():
        for gene_id in gene_ids:
            gene_type = gene_types.get(gene_id)
            if not gene_type:
                continue
            key = (gene_id, gene_type)
            tree_counts[key]["count"] += 1
            tree_counts[key]["web_id"] = gene_id
            tree_counts[key]["tree_id"] = gene_type
            if tree_counts[key]["first"] is None:
                tree_counts[key]["first"] = gene_type
            if gene_type not in names_by_pmid[pmid]:
                names_by_pmid[pmid].append(gene_type)

    return assemble_output(tree_counts, names_by_pmid, docs_meta, gene_ids_by_pmid, id_label="gene_id")


# ---------------------------------------------------------------------------
# Shared output assembly — same {tree, docs} shape as mesh_category_tree.json
# ---------------------------------------------------------------------------

def assemble_output(
    tree_counts: dict[tuple[str, str], dict[str, Any]],
    names_by_pmid: dict[str, list[str]],
    docs_meta: dict[str, dict[str, str]],
    gene_ids_by_pmid: dict[str, set[str]],
    id_label: str,
) -> dict[str, Any]:
    tree = [
        {id_label: gid, "web_id": info["web_id"], "tree_id": info["tree_id"], "count(*)": str(info["count"]), "first": info["first"]}
        for (gid, _path), info in sorted(tree_counts.items(), key=lambda kv: -kv[1]["count"])
    ]

    docs: list[dict[str, str]] = []
    for pmid, meta in docs_meta.items():
        if pmid not in gene_ids_by_pmid:
            continue  # only include docs that actually had at least one Gene mention
        names = names_by_pmid.get(pmid, [])
        subjects = " | ".join(names) + (" | " if names else "")
        docs.append({
            "pmid": pmid,
            "title_e": meta.get("title_e", ""),
            "journal": meta.get("journal", ""),
            "doi": meta.get("doi", ""),
            "year": meta.get("year", ""),
            "month": meta.get("month", ""),
            "day": meta.get("day", ""),
            "authors": meta.get("authors", ""),
            "subjects": subjects,
            "assigned_subjects1": subjects,
            "file_name": pmid,
        })

    return {"tree": tree, "docs": docs}


def load_docs_meta(doc_info_csv: str) -> dict[str, dict[str, str]]:
    meta: dict[str, dict[str, str]] = {}
    if not os.path.exists(doc_info_csv):
        print(f"WARNING: {doc_info_csv} not found — docs[] will be empty. Pass --doc-info-csv to point at it.")
        return meta
    with open(doc_info_csv, "r", encoding="utf-8", newline="") as fp:
        for row in csv_module.DictReader(fp):
            if row.get("fetch_status") != "ok":
                continue
            pmid = row.get("pmid", "")
            if pmid:
                meta[pmid] = row
    return meta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jsonl", default=DEFAULT_JSONL, help="Path to pubtator_records.jsonl.gz")
    parser.add_argument("--scratch-dir", default=SCRATCH_DIR, help="Where to download reference files (not committed)")
    parser.add_argument("--out-prefix", default=DEFAULT_OUT_PREFIX, help="Output files are <prefix>_<approach>.json")
    parser.add_argument("--doc-info-csv", default=DEFAULT_DOC_INFO_CSV)
    parser.add_argument("--tax-id", default="9606", help="NCBI taxonomy ID to filter gene2go/gene_info to (default: human)")
    parser.add_argument("--kegg-organism", default="hsa", help="KEGG organism code (default: hsa = human)")
    parser.add_argument("--limit", type=int, help="Only process the first N PMIDs, for testing")
    parser.add_argument(
        "--approach",
        default="go,hgnc,kegg,type",
        help="Comma-separated subset of go,hgnc,kegg,type (default: all four)",
    )
    parser.add_argument("--no-git-checkpoint", action="store_true")
    args = parser.parse_args()

    approaches = {a.strip() for a in args.approach.split(",") if a.strip()}
    unknown = approaches - {"go", "hgnc", "kegg", "type"}
    if unknown:
        parser.error(f"Unknown approach(es): {', '.join(sorted(unknown))}")

    if not os.path.exists(args.jsonl):
        parser.error(f"{args.jsonl} not found. Pass --jsonl to point at your pubtator_records.jsonl.gz cache.")

    os.makedirs(args.scratch_dir, exist_ok=True)
    _rows, gene_ids_by_pmid = collect_gene_mentions(args.jsonl, args.limit)
    docs_meta = load_docs_meta(args.doc_info_csv)

    builders = {
        "go": lambda: build_tree_go(gene_ids_by_pmid, docs_meta, args.scratch_dir, args.tax_id),
        "hgnc": lambda: build_tree_hgnc(gene_ids_by_pmid, docs_meta, args.scratch_dir),
        "kegg": lambda: build_tree_kegg(gene_ids_by_pmid, docs_meta, args.scratch_dir, args.kegg_organism),
        "type": lambda: build_tree_type(gene_ids_by_pmid, docs_meta, args.scratch_dir, args.tax_id),
    }

    for approach in ("go", "hgnc", "kegg", "type"):
        if approach not in approaches:
            continue
        print(f"\n=== Building gene tree via {approach} ===")
        try:
            output = builders[approach]()
        except Exception as exc:  # noqa: BLE001 - one approach failing shouldn't kill the others
            print(f"FAILED to build '{approach}' tree: {exc}")
            continue
        out_path = f"{args.out_prefix}_{approach}.json"
        ensure_parent_dir(out_path)
        with open(out_path, "w", encoding="utf-8") as fp:
            json.dump(output, fp, ensure_ascii=False, indent=2)
        print(f"Wrote {len(output['tree'])} tree entries, {len(output['docs'])} docs to {out_path}")
        if not args.no_git_checkpoint:
            git_checkpoint(out_path, message=f"Add gene category tree ({approach})")


if __name__ == "__main__":
    main()
