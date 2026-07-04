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
import json
import os
import re
import subprocess
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
DEFAULT_PUBTATOR_CACHE = os.path.join(DATA_DIR, "pubtator_records.jsonl")

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
    """Load the JSONL cache into memory: {pmid: record}. Later lines win, so a
    re-fetched PMID's newest record is used even if an older line exists."""
    cache: dict[str, Any] = {}
    if not os.path.exists(path):
        return cache
    with open(path, "r", encoding="utf-8") as fp:
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
    """Append one record. Cheap (no rewrite), and safe to commit incrementally —
    unlike one-file-per-PMID, this is a single file git can actually track."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fp:
        fp.write(json.dumps({"pmid": pmid, "record": record}, ensure_ascii=False) + "\n")


def write_rows(path: str, rows: list[dict[str, str]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def git_checkpoint(*paths: str, message: str) -> None:
    """Commit and push whatever's changed at `paths` so far, so a killed or
    cancelled run still leaves real progress behind instead of nothing. Never
    raises — a checkpoint failing shouldn't abort hours of fetching."""
    try:
        existing = [p for p in paths if os.path.exists(p)]
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
    rows: list[dict[str, str]] = []
    failed: dict[str, str] = {}

    print(f"PMIDs to process: {len(pmids)}")
    print(f"Output: {args.out}")
    print(f"PubTator cache: {args.pubtator_cache} ({len(pubtator_cache)} records loaded)")

    for start in range(0, len(pmids), args.batch_size):
        batch = pmids[start : start + args.batch_size]
        batch_num = start // args.batch_size + 1
        total_batches = (len(pmids) + args.batch_size - 1) // args.batch_size
        print(f"Batch {batch_num}/{total_batches}: {len(batch)} PMIDs", flush=True)

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
            else:
                pending.append(pmid)

        if not pending:
            continue

        records, reason = fetch_batch(pending, args.api_key)
        records_by_pmid = {str(rec.get("pmid", rec.get("id", ""))): rec for rec in records if rec}

        for pmid in pending:
            record = records_by_pmid.get(pmid)
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

        time.sleep(args.sleep)

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
