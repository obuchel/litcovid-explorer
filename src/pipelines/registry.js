export const PIPELINES = [
  {
    id: 'litcovid_docs',
    label: 'LitCovid document metadata',
    shortLabel: 'Documents',
    description:
      'Uploads a LitCovid search-results file (pmid / title_e / journal) and enriches every ' +
      'PMID with PubTator3 metadata: authors, DOI, journal, parsed date, title and abstract.',
    inputPath: 'data/search_results_litcovid.tsv',
    outputPath: 'data/doc_all_info.csv',
    workflowFile: 'fetch-doc-info.yml',
    acceptedFileTypes: '.tsv,.csv,.txt',
    inputHint:
      'The LitCovid "search results" export (tab-separated, with a commented header block) ' +
      'or a plain CSV with at least a pmid column.',
    columns: [
      { key: 'pmid', label: 'PMID', width: 100 },
      { key: 'title_e', label: 'Title', width: 380 },
      { key: 'authors', label: 'Authors', width: 220 },
      { key: 'journal', label: 'Journal', width: 160 },
      { key: 'date', label: 'Date', width: 100 },
      { key: 'doi', label: 'DOI', width: 160 },
      { key: 'pmcid', label: 'PMCID', width: 100 },
      { key: 'source', label: 'Source', width: 90 },
      { key: 'fetch_status', label: 'Status', width: 90 },
      { key: 'failure_reason', label: 'Failure reason', width: 140 },
      { key: 'abstract', label: 'Abstract', width: 420 },
    ],
    searchableColumns: ['pmid', 'title_e', 'authors', 'journal', 'doi'],
    defaultSortKey: 'pmid',
  },
  {
    id: 'mesh_tree',
    label: 'MeSH category tree',
    shortLabel: 'MeSH Tree',
    description:
      'Runs against whatever is already fetched in data/doc_all_info.csv and pubtator_records.jsonl.gz — ' +
      'no upload needed. Enriches citations via NIH iCite + an OpenAlex journal metric, then resolves every ' +
      'Disease/Chemical MeSH annotation to its full tree lineage and aggregates counts per branch.',
    noUpload: true,
    outputPath: 'data/mesh_category_tree.json',
    resultFormat: 'json-tree',
    workflowFile: 'build-mesh-tree.yml',
    columns: [
      { key: 'mesh_id', label: 'MeSH ID', width: 110 },
      { key: 'web_id', label: 'Web ID', width: 110 },
      { key: 'tree_id', label: 'Tree ID', width: 140 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['mesh_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'mesh_subjects',
    label: 'Enrich MeSH subjects',
    shortLabel: 'MeSH subjects',
    description:
      'Runs against the already-committed data/mesh_category_tree.json — no upload needed. Finds every ' +
      'document whose subjects field is still empty (PubTator3 usually just hasn\u2019t annotated it yet), ' +
      're-fetches just those PMIDs, resolves any Disease/Chemical MeSH IDs to their tree leaf terms, and ' +
      'writes subjects / assigned_subjects1 back in \u2014 without re-running the full tree build.',
    noUpload: true,
    outputPath: 'data/mesh_category_tree.json',
    resultFormat: 'json-docs',
    workflowFile: 'enrich_mesh_subjects.yml',
    columns: [
      { key: 'pmid', label: 'PMID', width: 100 },
      { key: 'title_e', label: 'Title', width: 360 },
      { key: 'journal', label: 'Journal', width: 160 },
      { key: 'subjects', label: 'Subjects', width: 320 },
      { key: 'number_citations', label: 'Citations', width: 100 },
    ],
    searchableColumns: ['pmid', 'title_e', 'subjects'],
    defaultSortKey: 'pmid',
  },
  {
    id: 'copy_categories',
    label: 'Copy reference categories',
    shortLabel: 'Ref. categories',
    description:
      'Runs against the already-committed data/mesh_category_tree.json \u2014 no upload needed. Downloads the ' +
      'whn-analytics.net reference JSON and copies its cat / hard_category / format fields in for any matching ' +
      'PMID, wherever this repo\u2019s own value is still blank. Never overwrites a value this repo already has.',
    noUpload: true,
    outputPath: 'data/mesh_category_tree.json',
    resultFormat: 'json-docs',
    workflowFile: 'copy_reference_categories.yml',
    columns: [
      { key: 'pmid', label: 'PMID', width: 100 },
      { key: 'title_e', label: 'Title', width: 340 },
      { key: 'cat', label: 'Category', width: 160 },
      { key: 'hard_category', label: 'Hard category', width: 160 },
      { key: 'format', label: 'Format', width: 120 },
    ],
    searchableColumns: ['pmid', 'title_e', 'cat', 'hard_category', 'format'],
    defaultSortKey: 'pmid',
  },
  {
    id: 'predict_categories',
    label: 'Predict categories',
    shortLabel: 'Predict cats',
    description:
      'Runs against the already-committed data/mesh_category_tree.json \u2014 no upload needed. Trains a text ' +
      'classifier on whichever docs already have hard_category / format (copied from the reference), then ' +
      'predicts values for the rest. Writes a SEPARATE file (mesh_category_tree_predicted.json) with ' +
      '*_predicted fields and confidence scores \u2014 never overwrites the reference-sourced ground truth. ' +
      'Accuracy is uneven (~61% / ~52% overall, much weaker on rare classes) \u2014 treat predictions as a ' +
      'first pass, not ground truth.',
    noUpload: true,
    outputPath: 'data/mesh_category_tree_predicted.json',
    resultFormat: 'json-docs',
    workflowFile: 'predict_categories.yml',
    columns: [
      { key: 'pmid', label: 'PMID', width: 100 },
      { key: 'title_e', label: 'Title', width: 320 },
      { key: 'hard_category_predicted', label: 'Predicted hard category', width: 160 },
      { key: 'hard_category_predicted_confidence', label: 'Confidence', width: 100 },
      { key: 'format_predicted', label: 'Predicted format', width: 160 },
      { key: 'format_predicted_confidence', label: 'Confidence', width: 100 },
    ],
    searchableColumns: ['pmid', 'title_e', 'hard_category_predicted', 'format_predicted'],
    defaultSortKey: 'pmid',
  },
  {
    id: 'authors',
    label: 'Author network (template)',
    shortLabel: 'Authors',
    description:
      'Template for splitting the authors column of doc_all_info.csv into a per-author table. ' +
      'See README.md -> "Adding a pipeline" for the pattern to follow.',
    comingSoon: true,
    inputPath: 'data/doc_all_info.csv',
    outputPath: 'data/authors.csv',
    workflowFile: null,
    columns: [],
    searchableColumns: [],
    defaultSortKey: null,
  },
];

export function getPipeline(id) {
  return PIPELINES.find((p) => p.id === id) ?? PIPELINES[0];
}
