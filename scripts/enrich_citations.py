"""
enrich_citations.py
--------------------
Adds two things doc_all_info.csv doesn't have on its own:

  1. Citation counts + NIH's Relative Citation Ratio, from NIH's iCite API
     (https://icite.od.nih.gov) — purpose-built for exactly this: bulk
     PMID -> citation metrics, no key required.

  2. A journal-level "2yr_mean_citedness" from OpenAlex — the closest free
     equivalent to a Journal Impact Factor (it is NOT the same metric as the
     proprietary Clarivate JCR Impact Factor; treat it as an impact-factor-
     *like* signal, not a drop-in replacement). This needs two passes:
     first resolve each PMID to its OpenAlex journal (source) ID, then look
     up that much smaller set of unique journals' stats.

Both APIs are called defensively: a failed batch/record never kills the
whole run (matching the rest of this pipeline's resilience), progress is
cached incrementally so a re-run only fetches what's missing, and results
are written to data/citation_enrichment.csv for build_mesh_annotations.py
to fold into docs[].
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

DEFAULT_DOC_INFO_CSV = os.path.join(DATA_DIR, "doc_all_info.csv")
DEFAULT_OUT_CSV = os.path.join(DATA_DIR, "citation_enrichment.csv")

ICITE_CACHE = os.path.join(DATA_DIR, "icite_cache.jsonl.gz")
OPENALEX_WORK_CACHE = os.path.join(DATA_DIR, "openalex_work_cache.jsonl.gz")
OPENALEX_SOURCE_CACHE = os.path.join(DATA_DIR, "openalex_source_cache.jsonl.gz")

ICITE_URL = "https://icite.od.nih.gov/api/pubs"
OPENALEX_WORKS_URL = "https://api.openalex.org/works/pmid:{pmid}"
OPENALEX_SOURCES_URL = "https://api.openalex.org/sources/{source_id}"

ICITE_BATCH_SIZE = 200  # current official limit (docs say up to 200 per request as of Sept 2025)

MAX_GIT_FILE_BYTES = 90 * 1024 * 1024  # stay under GitHub's hard 100MB push limit with headroom


def git_checkpoint(*paths: str, message: str) -> None:
    """Same safety net as fetch_pubmed_doc_info.py's git_checkpoint: commits
    progress periodically so a killed/timed-out run doesn't lose everything,
    and never attempts to commit a file over the safe size threshold (a >100MB
    push is a hard GitHub rejection that would otherwise take down the whole
    job). Never raises."""
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
# Generic gzip JSONL cache (same append-as-gzip-member trick used elsewhere
# in this project — real append, no decompress/rewrite needed)
# ---------------------------------------------------------------------------

def load_cache(path: str) -> dict[str, Any]:
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
            key = entry.get("key")
            if key:
                cache[key] = entry.get("value")
    return cache


def append_cache(path: str, key: str, value: Any) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = (json.dumps({"key": key, "value": value}, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "ab") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            gz.write(line)


# ---------------------------------------------------------------------------
# iCite
# ---------------------------------------------------------------------------

def fetch_icite_batch(pmids: list[str]) -> dict[str, dict[str, Any]]:
    """Returns {pmid: {citation_count, relative_citation_ratio, nih_percentile}}.
    Missing/unavailable PMIDs (e.g. too recent, or pre-1995) are simply absent
    from the result — that's normal, not an error."""
    url = f"{ICITE_URL}?pmids={','.join(pmids)}&fl=pmid,citation_count,relative_citation_ratio,nih_percentile"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"WARNING: iCite batch failed ({exc}), skipping this batch", flush=True)
        return {}

    records = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        return {}

    result = {}
    for rec in records:
        pmid = str(rec.get("pmid", ""))
        if pmid:
            result[pmid] = {
                "citation_count": rec.get("citation_count"),
                "relative_citation_ratio": rec.get("relative_citation_ratio"),
                "nih_percentile": rec.get("nih_percentile"),
            }
    return result


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------

def fetch_openalex_work(pmid: str, mailto: str) -> dict[str, str] | None:
    """Returns {"source_id": "S...", "source_name": "..."} or None if the
    work isn't in OpenAlex / has no primary_location.source."""
    url = OPENALEX_WORKS_URL.format(pmid=pmid) + "?select=primary_location"
    if mailto:
        url += f"&mailto={mailto}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        print(f"WARNING: OpenAlex work lookup failed for pmid {pmid}: {exc}", flush=True)
        return None
    except Exception as exc:
        print(f"WARNING: OpenAlex work lookup failed for pmid {pmid}: {exc}", flush=True)
        return None

    source = ((data.get("primary_location") or {}).get("source")) or {}
    source_id = source.get("id", "")
    if not source_id:
        return None
    return {"source_id": source_id.rsplit("/", 1)[-1], "source_name": source.get("display_name", "")}


def fetch_openalex_source(source_id: str, mailto: str) -> dict[str, Any] | None:
    """Returns {"display_name", "two_yr_mean_citedness"} or None."""
    url = OPENALEX_SOURCES_URL.format(source_id=source_id) + "?select=display_name,summary_stats"
    if mailto:
        url += f"&mailto={mailto}"
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"WARNING: OpenAlex source lookup failed for {source_id}: {exc}", flush=True)
        return None

    stats = data.get("summary_stats") or {}
    return {
        "display_name": data.get("display_name", ""),
        "two_yr_mean_citedness": stats.get("2yr_mean_citedness"),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc-info-csv", default=DEFAULT_DOC_INFO_CSV)
    parser.add_argument("--out", default=DEFAULT_OUT_CSV)
    parser.add_argument("--mailto", default="", help="Email for OpenAlex's polite pool (optional but recommended)")
    parser.add_argument("--limit", type=int, help="Only process the first N PMIDs, for testing")
    parser.add_argument("--sleep", type=float, default=0.15, help="Delay between OpenAlex per-PMID requests")
    parser.add_argument("--checkpoint-every", type=int, default=500, help="Commit cache files every N items (0 disables)")
    parser.add_argument("--no-checkpoint-commit", action="store_true", help="Write caches to disk but skip git commit/push")
    args = parser.parse_args()

    if not os.path.exists(args.doc_info_csv):
        print(f"ERROR: {args.doc_info_csv} not found.", flush=True)
        return

    with open(args.doc_info_csv, "r", encoding="utf-8", newline="") as fp:
        pmids = [row["pmid"] for row in csv.DictReader(fp) if row.get("fetch_status") == "ok" and row.get("pmid")]
    if args.limit:
        pmids = pmids[: args.limit]
    print(f"{len(pmids)} PMIDs to enrich")

    # --- iCite: citation counts, in batches ---
    icite_cache = load_cache(ICITE_CACHE)
    remaining = [p for p in pmids if p not in icite_cache]
    print(f"iCite: {len(pmids) - len(remaining)} already cached, {len(remaining)} to fetch")
    for start in range(0, len(remaining), ICITE_BATCH_SIZE):
        batch = remaining[start : start + ICITE_BATCH_SIZE]
        results = fetch_icite_batch(batch)
        for pmid in batch:
            value = results.get(pmid, {})  # empty dict if iCite has nothing for this pmid — still cached, not retried
            append_cache(ICITE_CACHE, pmid, value)
            icite_cache[pmid] = value
        batch_num = start // ICITE_BATCH_SIZE + 1
        print(f"iCite batch {batch_num}/{(len(remaining) + ICITE_BATCH_SIZE - 1) // ICITE_BATCH_SIZE} done", flush=True)
        if args.checkpoint_every and (batch_num * ICITE_BATCH_SIZE) % args.checkpoint_every == 0 and not args.no_checkpoint_commit:
            git_checkpoint(ICITE_CACHE, message=f"Checkpoint: iCite {len(icite_cache)}/{len(pmids)} [skip ci]")

    # --- OpenAlex pass 1: resolve each PMID to its journal (source) ---
    work_cache = load_cache(OPENALEX_WORK_CACHE)
    remaining = [p for p in pmids if p not in work_cache]
    print(f"OpenAlex work lookup: {len(pmids) - len(remaining)} already cached, {len(remaining)} to fetch")
    for i, pmid in enumerate(remaining, 1):
        value = fetch_openalex_work(pmid, args.mailto)
        append_cache(OPENALEX_WORK_CACHE, pmid, value)
        work_cache[pmid] = value
        time.sleep(args.sleep)
        if i % 200 == 0:
            print(f"OpenAlex work lookup: {i}/{len(remaining)}", flush=True)
        if args.checkpoint_every and i % args.checkpoint_every == 0 and not args.no_checkpoint_commit:
            git_checkpoint(OPENALEX_WORK_CACHE, message=f"Checkpoint: OpenAlex work lookup {len(work_cache)}/{len(pmids)} [skip ci]")

    # --- OpenAlex pass 2: journal-level stats, over the much smaller unique set ---
    unique_source_ids = {v["source_id"] for v in work_cache.values() if v and v.get("source_id")}
    source_cache = load_cache(OPENALEX_SOURCE_CACHE)
    remaining_sources = [s for s in unique_source_ids if s not in source_cache]
    print(f"OpenAlex source lookup: {len(unique_source_ids) - len(remaining_sources)} already cached, {len(remaining_sources)} unique journals to fetch")
    for i, source_id in enumerate(remaining_sources, 1):
        value = fetch_openalex_source(source_id, args.mailto)
        append_cache(OPENALEX_SOURCE_CACHE, source_id, value)
        source_cache[source_id] = value
        time.sleep(args.sleep)
        if i % 50 == 0:
            print(f"OpenAlex source lookup: {i}/{len(remaining_sources)}", flush=True)

    # --- Write the combined enrichment CSV ---
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    fieldnames = [
        "pmid", "citation_count", "relative_citation_ratio", "nih_percentile",
        "openalex_journal_name", "openalex_2yr_mean_citedness",
    ]
    with open(args.out, "w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        for pmid in pmids:
            icite = icite_cache.get(pmid) or {}
            work = work_cache.get(pmid) or {}
            source = source_cache.get(work.get("source_id", "")) or {} if work else {}
            writer.writerow({
                "pmid": pmid,
                "citation_count": icite.get("citation_count", ""),
                "relative_citation_ratio": icite.get("relative_citation_ratio", ""),
                "nih_percentile": icite.get("nih_percentile", ""),
                "openalex_journal_name": source.get("display_name", ""),
                "openalex_2yr_mean_citedness": source.get("two_yr_mean_citedness", ""),
            })
    print(f"Wrote {len(pmids)} rows to {args.out}")
    if not args.no_checkpoint_commit:
        git_checkpoint(
            ICITE_CACHE, OPENALEX_WORK_CACHE, OPENALEX_SOURCE_CACHE, args.out,
            message=f"Final: citation enrichment for {len(pmids)} PMIDs [skip ci]",
        )


if __name__ == "__main__":
    main()
