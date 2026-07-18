"""
predict_categories.py
-----------------------
Trains a simple text classifier per target field (hard_category, format —
"cat" was dropped, see below) on whichever docs already have that field
populated (copied from the whn-analytics.net reference by
copy_reference_categories.py), then predicts values for the remaining docs
that have a title/abstract/subjects but no reference value for that field.

"cat" was tried and dropped: it's a journal-abbreviation field, not a real
content category (689 near-unique classes, 5% accuracy) — not worth
predicting in its current form.

Predictions are written into NEW fields — hard_category_predicted /
format_predicted, each with a *_predicted_confidence score — never into the
same field copy_reference_categories.py populates. That keeps "copied from
the reference, ground truth" and "this project's own guess" from ever being
conflated in the same key, and writes to a SEPARATE output file
(mesh_category_tree_predicted.json) rather than overwriting
mesh_category_tree.json.

Features: TF-IDF over title_e + abstract (word + bigram), concatenated with
a multi-hot encoding of the doc's resolved MeSH subject leaf terms. Model:
one-vs-rest Logistic Regression per target, class_weight="balanced" so rare
classes aren't just ignored (their recall will still be poor with only a
handful of examples — expected and accepted for this first pass, not a bug
to chase). Real numbers on this repo's data: hard_category ~61% accuracy /
0.43 macro-F1, format ~52% / 0.38 macro-F1, both much stronger on
well-represented classes than on the long tail.

Requires: pip install scikit-learn scipy

Usage:
    python scripts/predict_categories.py                                    # train + report only, writes nothing
    python scripts/predict_categories.py --write --out data/mesh_category_tree_predicted.json
    python scripts/predict_categories.py --limit 3000 --write               # quick test run
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
from typing import Any

import numpy as np
from scipy import sparse
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import MultiLabelBinarizer

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_TREE_JSON = os.path.join(DATA_DIR, "mesh_category_tree.json")
DEFAULT_OUT = os.path.join(DATA_DIR, "mesh_category_tree_predicted.json")

TARGETS = ["hard_category", "format"]  # "cat" dropped — journal-abbreviation noise, not learnable (see eval: 5% accuracy)
MIN_LABELED_TO_TRAIN = 20  # below this, skip the target entirely rather than fit garbage

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



def doc_text(doc: dict[str, Any]) -> str:
    return f"{doc.get('title_e', '')} {doc.get('abstract', '')}".strip()


def doc_subjects(doc: dict[str, Any]) -> list[str]:
    return [s.strip() for s in (doc.get("subjects") or "").split("|") if s.strip()]


def build_features(docs, tfidf=None, mlb=None, fit=False):
    texts = [doc_text(d) for d in docs]
    subj_lists = [doc_subjects(d) for d in docs]
    if fit:
        tfidf = TfidfVectorizer(max_features=8000, stop_words="english", ngram_range=(1, 2), min_df=2)
        X_text = tfidf.fit_transform(texts)
        mlb = MultiLabelBinarizer(sparse_output=True)
        X_subj = mlb.fit_transform(subj_lists)
    else:
        X_text = tfidf.transform(texts)
        X_subj = mlb.transform(subj_lists)
    X = sparse.hstack([X_text, X_subj]).tocsr()
    return X, tfidf, mlb


def train_and_report(target: str, docs: list[dict[str, Any]]):
    labeled = [d for d in docs if d.get(target) and (doc_text(d) or doc_subjects(d))]
    if len(labeled) < MIN_LABELED_TO_TRAIN:
        print(f"Skipping {target}: only {len(labeled)} labeled examples (need >= {MIN_LABELED_TO_TRAIN})", flush=True)
        return None

    y = [d[target] for d in labeled]
    X, tfidf, mlb = build_features(labeled, fit=True)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    clf = LogisticRegression(max_iter=2000, class_weight="balanced")
    clf.fit(X_train, y_train)
    y_pred = clf.predict(X_test)

    print(f"\n=== {target} ===", flush=True)
    print(f"train n={X_train.shape[0]}, test n={X_test.shape[0]}, distinct classes={len(set(y))}", flush=True)
    print(classification_report(y_test, y_pred, zero_division=0), flush=True)
    return clf, tfidf, mlb


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tree-json", default=DEFAULT_TREE_JSON)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--write", action="store_true", help="Actually predict + write the output file. Without this flag, only trains and prints reports.")
    parser.add_argument("--limit", type=int, help="Only use the first N docs (training + prediction), for a quick test run")
    parser.add_argument("--no-checkpoint-commit", action="store_true", help="Write the output file to disk but skip git commit/push")
    args = parser.parse_args()

    if not os.path.exists(args.tree_json):
        print(f"ERROR: {args.tree_json} not found.", flush=True)
        return

    with open(args.tree_json, "r", encoding="utf-8") as fp:
        tree_data = json.load(fp)
    docs: list[dict[str, Any]] = tree_data.get("docs", [])
    if args.limit and args.limit < len(docs):
        random.seed(42)
        docs = random.sample(docs, args.limit)
    print(f"{len(docs)} total docs loaded", flush=True)

    models = {}
    for target in TARGETS:
        result = train_and_report(target, docs)
        if result:
            models[target] = result

    if not args.write:
        print("\n(no --write passed — nothing saved; this was training + evaluation only)", flush=True)
        return

    for target, (clf, tfidf, mlb) in models.items():
        pred_field = f"{target}_predicted"
        conf_field = f"{target}_predicted_confidence"
        to_predict = [d for d in docs if not d.get(target) and (doc_text(d) or doc_subjects(d))]
        if not to_predict:
            continue
        X, _, _ = build_features(to_predict, tfidf=tfidf, mlb=mlb, fit=False)
        probs = clf.predict_proba(X)
        best_idx = np.argmax(probs, axis=1)
        preds = clf.classes_[best_idx]
        confs = probs[np.arange(len(to_predict)), best_idx]
        for doc, pred, conf in zip(to_predict, preds, confs):
            doc[pred_field] = pred
            doc[conf_field] = round(float(conf), 3)
        print(f"{target}: predicted for {len(to_predict)} docs (field: {pred_field})", flush=True)

    tree_data["docs"] = docs
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(tree_data, fp, ensure_ascii=False, indent=2)
    print(f"\nWrote {args.out}", flush=True)

    if not args.no_checkpoint_commit:
        git_checkpoint(args.out, message=f"Predict hard_category/format for {len(docs)} docs [skip ci]")


if __name__ == "__main__":
    main()
