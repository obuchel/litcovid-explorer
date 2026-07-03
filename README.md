# LitCovid Doc Info Explorer

A small React app + GitHub Actions pipeline for enriching a LitCovid search-results
export with PubTator3 metadata (authors, DOI, journal, parsed date, title, abstract),
with an interactive table and CSV download.

## Why this shape

The actual fetching (thousands of PMIDs against the PubTator3 API) needs to keep
running even if you close the browser tab, and calling PubTator3 straight from browser
JS is unreliable because of CORS. So the work is split:

- **`scripts/fetch_pubmed_doc_info.py`** — the real pipeline, unmodified server-side
  Python. It runs inside **GitHub Actions**, not the browser, so there's no CORS issue
  and no dependency on the tab staying open.
- **`.github/workflows/fetch-doc-info.yml`** — runs that script on a schedule
  (weekly by default — edit the cron), whenever `data/search_results_litcovid.tsv`
  changes, or on demand via `workflow_dispatch`.
- **The React app** (this repo, deployed to GitHub Pages) — a thin client that commits
  an uploaded file into `data/`, triggers the workflow via the GitHub API, polls it to
  completion, then fetches and displays `data/doc_all_info.csv`.

Because the run lives on GitHub's infrastructure, you can start it from the app and
close the tab — it finishes regardless, and `data/doc_all_info.csv` is committed back
to the repo when it's done. Reopen the app any time and click **Load latest results**.

## One-time setup

1. Push this repo to GitHub.
2. **Enable GitHub Pages**: repo Settings → Pages → Source → "GitHub Actions". The
   `deploy-pages.yml` workflow will build and publish the app on every push to `main`.
3. **Create a personal access token** (Settings → Developer settings → Fine-grained
   tokens → Generate new token), scoped to just this repository, with:
   - Contents: Read and write
   - Actions: Read and write
4. Open the deployed app, fill in the owner / repo / branch / token fields. The token
   is kept only in the page's memory for that session — it isn't saved anywhere,
   including between reloads. You'll re-enter it each visit; that's intentional.

## Using it

1. Pick a pipeline in the left sidebar (currently: **Documents**).
2. Drop in a LitCovid search-results file (or a plain CSV with a `pmid` column).
3. Optionally set a **limit** for a quick test run, or check **re-fetch cached PMIDs**
   to force a full refresh instead of reusing the PubTator3 cache.
4. Click **Commit & run**. The app commits your file, starts the workflow, and shows
   live status. When it finishes, the table and download button populate automatically.
5. Anytime later, **Load latest results** re-fetches `data/doc_all_info.csv` from the
   repo without starting a new run — useful after the weekly scheduled run, or after
   closing and reopening the tab mid-run.

## Data layout in the repo

```
data/
  search_results_litcovid.tsv   <- input, replaced by each upload
  doc_all_info.csv              <- output, what the table/download show
  failed_pmids_all_info.txt     <- PMIDs PubTator3 didn't return a record for
```

The per-PMID PubTator3 JSON cache is kept via `actions/cache` between workflow runs
(not committed to git — thousands of small files would bloat the repo), so re-runs
only fetch PMIDs that are new or previously failed, unless you check "re-fetch cached
PMIDs."

## Adding a pipeline (authors, citations, ...)

The architecture is meant to grow. To add a new derived dataset:

1. Add `scripts/<name>.py` — reads whatever input it needs (e.g.
   `data/doc_all_info.csv`) and writes `data/<name>.csv`.
2. Add `.github/workflows/<name>.yml`, following the pattern in
   `fetch-doc-info.yml` (checkout → setup-python → run script → commit).
3. Add an entry to `src/pipelines/registry.js` with `id`, `label`, `inputPath`,
   `outputPath`, `workflowFile`, and the `columns` you want the table to show.

Nothing else in the app needs to change — the sidebar, upload flow, run polling, and
table are all generic over whatever's in the registry. `src/pipelines/registry.js`
already has an `authors` entry marked `comingSoon: true` as a starting template.

## Local development

```
npm install
npm run dev
```

The dev server talks to the same GitHub API endpoints as production, so you can test
the full commit → dispatch → poll → results flow locally as long as you fill in real
repo settings and a token.

## Security note

The token you paste into the app has write access to this repo's contents and can
trigger workflow runs — treat it like any other credential. Prefer a fine-grained
token scoped to just this repository over a classic PAT with broad `repo` scope, and
revoke it from GitHub's settings when you're done with a session if you're on a shared
machine.
# litcovid-explorer
