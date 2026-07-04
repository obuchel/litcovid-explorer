"""
fetch_pubmed_doc_info.py
------------------------
Fetch rich PubMed/PubTator metadata for every PMID in a LitCovid search TSV
and write it to a separate CSV file.

This is intentionally separate from docs3.csv: it reads the LitCovid TSV and
optionally writes/uses cached PubTator BioC-JSON files, but the main output is
a standalone metadata table.

Usage:
    python fetch_pubmed_doc_info.py

    python fetch_pubmed_doc_info.py \
      --tsv search.results.litcovid.tsv \
      --out docs_all_info.csv

    # Retry only PMIDs that previously failed:
    python fetch_pubmed_doc_info.py --retry-failed failed_pmids_all_info.txt
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

import pandas as pd


# ---------------------------------------------------------------------------
# Defaults: edit these or pass command-line args.
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_TSV = os.path.join(DATA_DIR, "search_results_litcovid.tsv")
DEFAULT_DOCS3 = os.path.join(DATA_DIR, "docs3.csv")
DEFAULT_OUT = os.path.join(DATA_DIR, "doc_all_info.csv")
DEFAULT_FAILED = os.path.join(DATA_DIR, "failed_pmids_all_info.txt")
DEFAULT_PUBTATOR_CACHE = os.path.join(DATA_DIR, "pubtator_records.jsonl.gz")

TSV_SKIPROWS = -1  # -1 = auto-detect: skip leading '#' comment / blank lines
BATCH_SIZE = 20
SLEEP_BETWEEN = 1.0
MAX_RETRIES = 3
BACKOFF_BASE = 2.0

PUBTATOR_URL = (
    "https://www.ncbi.nlm.nih.gov/research/pubtator-api/publications/export/biocjson"
    "?pmids={pmids}"
)

MONTHS = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
}

OUTPUT_COLUMNS = [
    "pmid",
    "id",
    "pmcid",
    "date",
    "year",
    "month",
    "day",
    "journal",
    "doi",
    "authors",
    "title_e",
    "abstract",
    "litcovid_title_e",
    "litcovid_journal",
    "file_name",
    "source",
    "fetch_status",
    "failure_reason",
    "failed",
]


def read_litcovid_tsv(path: str, skiprows: int) -> pd.DataFrame:
    sep = "," if path.lower().endswith(".csv") else "\t"

    if skiprows < 0:
        skiprows = 0
        with open(path, "r", encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if line.startswith("#") or not line.strip():
                    skiprows += 1
                    continue
                break

    df = pd.read_csv(path, sep=sep, skiprows=skiprows, engine="python", quoting=csv.QUOTE_NONE)
    if "pmid" not in df.columns:
        raise ValueError(f"{path} does not contain a 'pmid' column")
    df["pmid"] = df["pmid"].astype(str)
    return df


def read_existing_dates(path: str) -> dict[str, dict[str, str]]:
    if not path or not os.path.exists(path):
        return {}

    df = pd.read_csv(path, dtype=str)
    id_col = "pmid" if "pmid" in df.columns else "id"
    dates: dict[str, dict[str, str]] = {}
    for _, row in df.iterrows():
        pmid = str(row.get(id_col, "")).strip()
        if not pmid or pmid == "nan":
            continue

        year = clean_number(row.get("year", ""))
        month = clean_number(row.get("month", "")).zfill(2) if clean_number(row.get("month", "")) else ""
        day = clean_number(row.get("day", "")).zfill(2) if clean_number(row.get("day", "")) else ""
        date = clean_number(row.get("date", ""))
        if not date and year:
            date = "-".join([x for x in [year, month, day] if x])
        if not date and not year:
            continue
        dates[pmid] = {"date": date, "year": year, "month": month, "day": day}
    return dates


def clean_number(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    if text.endswith(".0"):
        text = text[:-2]
    return text


def parse_date_from_journal(journal: str) -> dict[str, str]:
    result = {"date": "", "year": "", "month": "", "day": ""}
    if not journal:
        return result

    parts = journal.split(";")
    if len(parts) < 2:
        year_match = re.search(r"\b(19|20)\d{2}\b", journal)
        if year_match:
            result.update({"date": year_match.group(0), "year": year_match.group(0)})
        return result

    raw = parts[1].strip()
    raw = raw.split(". ")[0].strip()
    raw = raw.split(" doi:")[0].strip()
    raw = raw.split(" PMID:")[0].strip()

    month_pattern = "|".join(MONTHS)
    month_match = re.search(
        rf"\b((?:19|20)\d{{2}})\s*({month_pattern})\s*(\d{{1,2}})?\b",
        raw,
    )
    if month_match:
        year = month_match.group(1)
        month = MONTHS[month_match.group(2)]
        day = (month_match.group(3) or "01").zfill(2)
        return {"date": f"{year}{month}{day}", "year": year, "month": month, "day": day}

    for month_name, month_num in MONTHS.items():
        raw = raw.replace(month_name, month_num)

    compact = re.sub(r"[^0-9]", "", raw)
    if len(compact) >= 8:
        result["date"] = compact[:8]
        result["year"] = compact[:4]
        result["month"] = compact[4:6]
        result["day"] = compact[6:8]
    elif len(compact) == 6:
        result["date"] = compact
        result["year"] = compact[:4]
        result["month"] = compact[4:6]
        result["day"] = "01"
    elif len(compact) == 4:
        result["date"] = compact
        result["year"] = compact
        result["month"] = "01"
        result["day"] = "01"

    return result


def parse_record_date(value: Any) -> dict[str, str]:
    text = str(value or "").strip()
    match = re.match(r"^((?:19|20)\d{2})-(\d{2})-(\d{2})", text)
    if not match:
        return {}
    year, month, day = match.groups()
    return {"date": f"{year}{month}{day}", "year": year, "month": month, "day": day}


def get_passages(record: dict[str, Any]) -> list[dict[str, Any]]:
    passages = record.get("passages", [])
    return passages if isinstance(passages, list) else []


def first_passage(record: dict[str, Any]) -> dict[str, Any]:
    passages = get_passages(record)
    return passages[0] if passages else {}


def passage_text_by_type(record: dict[str, Any], wanted_type: str) -> str:
    for passage in get_passages(record):
        infons = passage.get("infons", {}) or {}
        if str(infons.get("type", "")).lower() == wanted_type.lower():
            return str(passage.get("text", "") or "")
    return ""


def extract_doi(record: dict[str, Any], journal: str) -> str:
    for key in ("doi", "article-id_doi"):
        value = record.get(key)
        if value:
            return str(value)

    match = re.search(r"doi:\s*([^;\s]+)", journal or "", flags=re.IGNORECASE)
    return match.group(1).strip() if match else ""


def extract_record_info(
    pmid: str,
    record: dict[str, Any] | None,
    litcovid_row: dict[str, Any],
    existing_dates: dict[str, dict[str, str]],
    source: str,
    status: str,
    reason: str = "",
) -> dict[str, str]:
    row = {key: "" for key in OUTPUT_COLUMNS}
    row["pmid"] = pmid
    row["file_name"] = pmid
    row["source"] = source
    row["fetch_status"] = status
    row["failure_reason"] = reason
    row["failed"] = "1" if status == "failed" else "0"
    row["litcovid_title_e"] = str(litcovid_row.get("title_e", "") or "")
    row["litcovid_journal"] = str(litcovid_row.get("journal", "") or "")

    if not record:
        row["title_e"] = row["litcovid_title_e"]
        row["journal"] = row["litcovid_journal"]
        return row

    passage0 = first_passage(record)
    infons = passage0.get("infons", {}) if isinstance(passage0.get("infons", {}), dict) else {}

    row["id"] = str(record.get("id", pmid) or pmid)
    row["pmcid"] = str(record.get("pmcid", "") or infons.get("article-id_pmc", "") or "")
    row["journal"] = str(infons.get("journal", "") or record.get("journal", "") or row["litcovid_journal"])
    row["doi"] = extract_doi(record, row["journal"])
    row["authors"] = str(infons.get("authors", "") or record.get("authors", "") or "")

    title = passage_text_by_type(record, "title")
    abstract = passage_text_by_type(record, "abstract")
    passages = get_passages(record)
    if not title and passages:
        title = str(passages[0].get("text", "") or "")
    if not abstract and len(passages) > 1:
        abstract = str(passages[1].get("text", "") or "")

    row["title_e"] = title or row["litcovid_title_e"]
    row["abstract"] = abstract

    date_info = (
        existing_dates.get(pmid)
        or parse_record_date(record.get("date"))
        or parse_date_from_journal(row["journal"])
    )
    if not date_info.get("year"):
        date_info["year"] = clean_number(record.get("year", ""))
    row.update({key: str(date_info.get(key, "") or "") for key in ("date", "year", "month", "day")})
    return row


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
            req = urllib.request.Request(url, headers={"User-Agent": "fetch_pubmed_doc_info/1.0"})
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
    """Load the gzip-compressed JSONL cache into memory: {pmid: record}.
    Later lines win, so a re-fetched PMID's newest record is used even if an
    older line exists. Falls back to reading an old uncompressed .jsonl file
    at the same base name once, for repos that had the cache from before
    compression was added."""
    cache: dict[str, Any] = {}

    def _consume(fp):
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

    if os.path.exists(path):
        with gzip.open(path, "rt", encoding="utf-8") as fp:
            _consume(fp)

    if path.endswith(".gz"):
        legacy_path = path[:-3]
        if os.path.exists(legacy_path):
            print(f"Loading legacy uncompressed cache too: {legacy_path}")
            with open(legacy_path, "r", encoding="utf-8") as fp:
                _consume(fp)

    return cache


def append_pubtator_cache(path: str, pmid: str, record: dict[str, Any]) -> None:
    """Append one record as its own gzip member (Python's gzip reader
    transparently decompresses concatenated members, so this is a real
    append — no need to decompress-rewrite-recompress the whole file)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    line = (json.dumps({"pmid": pmid, "record": record}, ensure_ascii=False) + "\n").encode("utf-8")
    with open(path, "ab") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
            gz.write(line)


MAX_GIT_FILE_BYTES = 90 * 1024 * 1024  # stay under GitHub's hard 100MB push limit with headroom


def write_rows(path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def running_interactively() -> bool:
    """True only for a real local terminal. isatty() alone isn't a reliable
    enough guard in CI — some runners report unexpected tty state — so this
    also explicitly rules out any environment exporting common CI markers,
    to make absolutely sure this never blocks forever waiting for input()
    that will never come in an automated run."""
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return False
    return sys.stdin.isatty()


def read_existing_output(path: str) -> dict[str, dict[str, str]]:
    """Read a previous run's output CSV, keyed by pmid. Used to resume: any
    pmid already marked fetch_status=ok here doesn't need to be redone."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8", newline="") as fp:
        return {row["pmid"]: row for row in csv.DictReader(fp) if row.get("pmid")}


def git_checkpoint(*paths: str, message: str) -> None:
    """Commit and push whatever's changed at `paths` so far, so a killed or
    cancelled run still leaves real progress behind instead of nothing. Never
    raises — a checkpoint failing shouldn't abort hours of fetching.

    Also never commits a file over MAX_GIT_FILE_BYTES: GitHub hard-rejects any
    push containing a file over 100MB, and that rejection would otherwise take
    down the *entire* job (this is what caused the real workflow failures —
    not a hang), so oversized files are skipped with a loud warning instead."""
    try:
        existing = []
        for p in paths:
            if not os.path.exists(p):
                continue
            size = os.path.getsize(p)
            if size > MAX_GIT_FILE_BYTES:
                print(
                    f"WARNING: {p} is {size / 1024 / 1024:.1f}MB, over the "
                    f"{MAX_GIT_FILE_BYTES / 1024 / 1024:.0f}MB safe limit — "
                    "skipping it this checkpoint so the push doesn't fail outright. "
                    "This file needs to shrink (e.g. compression, sharding) or move to Git LFS.",
                    flush=True,
                )
                continue
            existing.append(p)
        if not existing:
            return
        subprocess.run(["git", "add", *existing], cwd=BASE_DIR, check=True)
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=BASE_DIR)
        if diff.returncode == 0:
            return  # nothing changed since the last checkpoint
        subprocess.run(["git", "commit", "-m", message], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "pull", "--rebase", "--autostash"], cwd=BASE_DIR, check=True)
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        print(f"Checkpoint committed: {message}", flush=True)
    except subprocess.CalledProcessError as exc:
        print(f"Checkpoint commit failed (continuing the run): {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch all PubMed/PubTator document metadata into a separate CSV.")
    parser.add_argument("--tsv", default=DEFAULT_TSV, help="LitCovid TSV/CSV path")
    parser.add_argument("--skiprows", type=int, default=TSV_SKIPROWS, help="Header rows to skip (-1 = auto-detect)")
    parser.add_argument("--docs3", default=DEFAULT_DOCS3, help="Optional docs3.csv date cache path")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output CSV path")
    parser.add_argument(
        "--pubtator-cache",
        default=DEFAULT_PUBTATOR_CACHE,
        help="JSONL file caching every PubTator record fetched, keyed by PMID",
    )
    parser.add_argument("--failed-out", default=DEFAULT_FAILED, help="Failed PMID output path")
    parser.add_argument("--retry-failed", help="Read PMIDs from this file instead of the TSV")
    parser.add_argument("--api-key", default="", help="Optional NCBI API key")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--sleep", type=float, default=SLEEP_BETWEEN)
    parser.add_argument("--limit", type=int, help="Fetch only the first N PMIDs for testing")
    parser.add_argument("--force-refresh", action="store_true", help="Re-fetch records even if cached JSON exists")
    parser.add_argument(
        "--checkpoint-every",
        type=int,
        default=10,
        help="Commit & push doc_all_info.csv and the PubTator cache every N batches (0 disables)",
    )
    parser.add_argument("--no-checkpoint-commit", action="store_true", help="Write checkpoints to disk but skip git commit/push")
    args = parser.parse_args()

    tsv_df = read_litcovid_tsv(args.tsv, args.skiprows)
    litcovid_by_pmid = {str(row["pmid"]): row.to_dict() for _, row in tsv_df.iterrows()}

    if args.retry_failed:
        with open(args.retry_failed, "r", encoding="utf-8") as fp:
            pmids = [line.strip() for line in fp if line.strip()]
    else:
        pmids = tsv_df["pmid"].astype(str).tolist()

    if args.limit:
        pmids = pmids[: args.limit]

    existing_dates = read_existing_dates(args.docs3)
    pubtator_cache = {} if args.force_refresh else load_pubtator_cache(args.pubtator_cache)

    existing_output = {} if args.force_refresh else read_existing_output(args.out)
    already_ok = {p for p, row in existing_output.items() if row.get("fetch_status") == "ok"}
    remaining_pmids = [p for p in pmids if p not in already_ok]

    print(f"Total PMIDs in input: {len(pmids)}")
    print(f"Already completed in {args.out}: {len(already_ok)}")
    print(f"Remaining to process: {len(remaining_pmids)}")
    print(f"PubTator cache: {args.pubtator_cache} ({len(pubtator_cache)} records loaded)")

    if not remaining_pmids and not args.force_refresh:
        print(f"Every PMID already has a successful row in {args.out} — nothing to fetch.")
        if running_interactively():
            answer = input("Re-fetch everything from scratch anyway? [y/N]: ").strip().lower()
            if answer in ("y", "yes"):
                print("Restarting from scratch as requested.")
                args.force_refresh = True
                existing_output = {}
                already_ok = set()
                remaining_pmids = list(pmids)
                pubtator_cache = {}
            else:
                print("Leaving existing output untouched. Exiting.")
                return
        else:
            print("Non-interactive run (e.g. GitHub Actions) — leaving existing output untouched.")
            print("Pass --force-refresh explicitly if you really want to redo everything.")
            return

    # rows starts with everything already-successful from a prior run, so a
    # resumed run's checkpoints/output are always a complete, correct snapshot
    # — not just the delta being processed this time.
    rows: list[dict[str, str]] = [existing_output[p] for p in pmids if p in already_ok]
    failed: dict[str, str] = {}

    for start in range(0, len(remaining_pmids), args.batch_size):
        batch = remaining_pmids[start : start + args.batch_size]
        batch_num = start // args.batch_size + 1
        total_batches = (len(remaining_pmids) + args.batch_size - 1) // args.batch_size
        print(f"Batch {batch_num}/{total_batches}: {len(batch)} PMIDs", flush=True)

        handled_this_batch: set[str] = set()
        try:
            pending: list[str] = []
            for pmid in batch:
                cached = pubtator_cache.get(pmid) if not args.force_refresh else None
                if cached:
                    rows.append(
                        extract_record_info(
                            pmid,
                            cached,
                            litcovid_by_pmid.get(pmid, {}),
                            existing_dates,
                            source="cache",
                            status="ok",
                        )
                    )
                    handled_this_batch.add(pmid)
                else:
                    pending.append(pmid)

            if pending:
                records, reason = fetch_batch(pending, args.api_key)
                records_by_pmid = {str(rec.get("pmid", rec.get("id", ""))): rec for rec in records if rec}

                for pmid in pending:
                    record = records_by_pmid.get(pmid)
                    try:
                        if record:
                            append_pubtator_cache(args.pubtator_cache, pmid, record)
                            pubtator_cache[pmid] = record
                            rows.append(
                                extract_record_info(
                                    pmid,
                                    record,
                                    litcovid_by_pmid.get(pmid, {}),
                                    existing_dates,
                                    source="pubtator",
                                    status="ok",
                                )
                            )
                        else:
                            failure_reason = reason or "absent_from_response"
                            failed[pmid] = failure_reason
                            rows.append(
                                extract_record_info(
                                    pmid,
                                    None,
                                    litcovid_by_pmid.get(pmid, {}),
                                    existing_dates,
                                    source="litcovid",
                                    status="failed",
                                    reason=failure_reason,
                                )
                            )
                    except Exception as exc:  # a single malformed record must not kill the whole run
                        print(f"WARNING: failed to process pmid {pmid}: {exc}", flush=True)
                        failed[pmid] = f"processing_error:{exc}"
                        rows.append(
                            extract_record_info(
                                pmid,
                                None,
                                litcovid_by_pmid.get(pmid, {}),
                                existing_dates,
                                source="litcovid",
                                status="failed",
                                reason=f"processing_error:{exc}",
                            )
                        )
                    finally:
                        handled_this_batch.add(pmid)

            time.sleep(args.sleep)
        except Exception as exc:  # a whole bad batch (e.g. unexpected API shape) must not kill the run either
            print(f"WARNING: batch {batch_num} failed entirely, continuing: {exc}", flush=True)
            for pmid in batch:
                if pmid not in handled_this_batch:
                    failed[pmid] = f"batch_error:{exc}"
                    rows.append(
                        extract_record_info(
                            pmid, None, litcovid_by_pmid.get(pmid, {}), existing_dates,
                            source="litcovid", status="failed", reason=f"batch_error:{exc}",
                        )
                    )

        # Always write the CSV to disk so progress survives a crash even between
        # checkpoints; only commit/push every `checkpoint_every` batches to avoid
        # hammering the git remote on every single batch.
        write_rows(args.out, rows)
        if args.checkpoint_every and batch_num % args.checkpoint_every == 0 and not args.no_checkpoint_commit:
            git_checkpoint(
                args.out,
                args.pubtator_cache,
                message=f"Checkpoint: {len(rows)}/{len(pmids)} PMIDs processed [skip ci]",
            )

    # Safety net: ensure every PMID from the source list has exactly one row.
    # Catches any edge cases where a PMID was neither processed nor recorded as failed.
    written_pmids = {row["pmid"] for row in rows}
    missing = [p for p in pmids if p not in written_pmids]
    if missing:
        print(f"WARNING: {len(missing)} PMIDs had no row — adding as failed.", flush=True)
        for pmid in missing:
            reason = "missing_from_output"
            failed[pmid] = reason
            rows.append(
                extract_record_info(
                    pmid,
                    None,
                    litcovid_by_pmid.get(pmid, {}),
                    existing_dates,
                    source="litcovid",
                    status="failed",
                    reason=reason,
                )
            )

    write_rows(args.out, rows)
    print(f"Wrote {len(rows)} rows to {args.out} (expected {len(pmids)})")
    if not args.no_checkpoint_commit:
        git_checkpoint(args.out, args.pubtator_cache, message=f"Final: {len(rows)}/{len(pmids)} PMIDs processed [skip ci]")
    if len(rows) != len(pmids):
        print(f"WARNING: row count mismatch — {len(rows)} rows vs {len(pmids)} PMIDs", flush=True)

    if failed:
        timestamped_failed = args.failed_out.replace(".txt", f"_{datetime.now():%Y%m%d_%H%M%S}.txt")
        for path in (args.failed_out, timestamped_failed):
            with open(path, "w", encoding="utf-8") as fp:
                fp.write("\n".join(failed.keys()))
        print(f"Failed PMIDs: {len(failed)}")
        print(f"Saved failed list to {args.failed_out}")
    else:
        if os.path.exists(args.failed_out):
            os.remove(args.failed_out)
        print("No failed PMIDs.")


if __name__ == "__main__":
    main()
