"""
sync_public_data.py
---------------------
Copies every file in data/ into public/data/. The standalone dashboard HTML
files in public/ (long_covid_dashboard_v2_enhanced*.html, gene_category_trees.html)
fetch their data from a relative ./data/ path once deployed to GitHub Pages,
which means they read whatever is sitting in public/data/ at build time —
not the live data/ directory the pipelines actually write to. This keeps
that snapshot current.

Only copies (never deletes): a file that exists in public/data/ but not in
data/ (e.g. mesh_annotations.csv, which is gitignored at the repo root but
committed under public/ for the dashboards) is left alone.

Also writes public/data/_sync_manifest.json — a {docs: [...]} list of what
was copied, with size and a timestamp — so this has a real result to show
when it's run as its own pipeline in the app (rather than only ever running
silently as a step inside deploy-pages.yml).

Usage:
    python scripts/sync_public_data.py
    python scripts/sync_public_data.py --data-dir data --public-data-dir public/data
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_PUBLIC_DATA_DIR = os.path.join(BASE_DIR, "public", "data")
MANIFEST_NAME = "_sync_manifest.json"


def sync_data_to_public(data_dir: str = DEFAULT_DATA_DIR, public_data_dir: str = DEFAULT_PUBLIC_DATA_DIR) -> list[str]:
    """Copies every file under data_dir into public_data_dir, preserving any
    subdirectory structure. Overwrites files that already exist there;
    leaves anything in public_data_dir with no counterpart in data_dir
    untouched. Returns the list of relative paths copied."""
    if not os.path.isdir(data_dir):
        raise FileNotFoundError(f"{data_dir} not found")

    copied: list[str] = []
    for root, _dirs, files in os.walk(data_dir):
        rel_dir = os.path.relpath(root, data_dir)
        dest_dir = os.path.join(public_data_dir, rel_dir) if rel_dir != "." else public_data_dir
        os.makedirs(dest_dir, exist_ok=True)
        for name in files:
            src = os.path.join(root, name)
            dest = os.path.join(dest_dir, name)
            shutil.copy2(src, dest)
            rel_path = os.path.relpath(src, data_dir)
            copied.append(rel_path)
    return copied


def write_manifest(copied: list[str], public_data_dir: str = DEFAULT_PUBLIC_DATA_DIR) -> str:
    """Writes public_data_dir/_sync_manifest.json as {"docs": [...]}, matching
    the resultFormat: 'json-docs' shape the app already knows how to render
    as a table (see mesh_subjects / copy_categories in registry.js)."""
    synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    docs = []
    for rel_path in sorted(copied):
        full_path = os.path.join(public_data_dir, rel_path)
        size_bytes = os.path.getsize(full_path) if os.path.exists(full_path) else 0
        docs.append({"file": rel_path, "size_bytes": str(size_bytes), "synced_at": synced_at})
    manifest_path = os.path.join(public_data_dir, MANIFEST_NAME)
    with open(manifest_path, "w", encoding="utf-8") as fp:
        json.dump({"docs": docs}, fp, ensure_ascii=False, indent=2)
    return manifest_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--public-data-dir", default=DEFAULT_PUBLIC_DATA_DIR)
    parser.add_argument("--no-manifest", action="store_true", help="Skip writing _sync_manifest.json")
    args = parser.parse_args()

    copied = sync_data_to_public(args.data_dir, args.public_data_dir)
    print(f"Copied {len(copied)} file(s) from {args.data_dir} to {args.public_data_dir}")
    for path in copied:
        print(f"  {path}")

    if not args.no_manifest:
        manifest_path = write_manifest(copied, args.public_data_dir)
        print(f"Wrote manifest to {manifest_path}")


if __name__ == "__main__":
    main()
