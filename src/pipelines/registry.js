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
    id: 'split_diseases',
    label: 'Category tree \u2014 Diseases',
    shortLabel: 'Diseases tree',
    description:
      'Splits data/mesh_category_tree.json by top-level MeSH category, mirroring how whn-analytics.net splits ' +
      'its Diseases-only and Chemicals-only reference files. This entry shows the Diseases split; running it ' +
      'also produces the Chemicals and "other" (Anatomy / Psychiatry / etc.) splits in the same run.',
    noUpload: true,
    outputPath: 'data/mesh_category_tree_diseases.json',
    resultFormat: 'json-tree',
    workflowFile: 'split_category_trees.yml',
    columns: [
      { key: 'mesh_id', label: 'MeSH ID', width: 110 },
      { key: 'tree_id', label: 'Tree ID', width: 140 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['mesh_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'split_chemicals',
    label: 'Category tree \u2014 Chemicals',
    shortLabel: 'Chemicals tree',
    description:
      'Same run as "Category tree \u2014 Diseases" \u2014 this entry shows the Chemicals and Drugs split of ' +
      'data/mesh_category_tree.json.',
    noUpload: true,
    outputPath: 'data/mesh_category_tree_chemicals.json',
    resultFormat: 'json-tree',
    workflowFile: 'split_category_trees.yml',
    columns: [
      { key: 'mesh_id', label: 'MeSH ID', width: 110 },
      { key: 'tree_id', label: 'Tree ID', width: 140 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['mesh_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'split_other',
    label: 'Category tree \u2014 Other',
    shortLabel: 'Other tree',
    description:
      'Same run as "Category tree \u2014 Diseases" \u2014 this entry shows everything outside Diseases/Chemicals ' +
      '(Anatomy, Psychiatry and Psychology, Phenomena and Processes, etc.), lumped together unless the split-all ' +
      'option was used.',
    noUpload: true,
    outputPath: 'data/mesh_category_tree_other.json',
    resultFormat: 'json-tree',
    workflowFile: 'split_category_trees.yml',
    columns: [
      { key: 'mesh_id', label: 'MeSH ID', width: 110 },
      { key: 'tree_id', label: 'Tree ID', width: 140 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['mesh_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'gene_tree_go',
    label: 'Gene tree \u2014 Gene Ontology',
    shortLabel: 'Genes (GO)',
    description:
      'Runs against data/pubtator_records.jsonl.gz \u2014 no upload needed. Pulls out Gene-type PubTator ' +
      'annotations (build_mesh_annotations.py discards these, since genes use NCBI Entrez IDs rather than ' +
      'MeSH) and classifies them via Gene Ontology: three root namespaces (Biological Process / Molecular ' +
      'Function / Cellular Component) with GO\u2019s own is_a hierarchy underneath. Running it also produces ' +
      'the HGNC, KEGG, and type_of_gene trees in the same run.',
    noUpload: true,
    outputPath: 'data/gene_category_tree_go.json',
    resultFormat: 'json-tree',
    workflowFile: 'extract-genes.yml',
    columns: [
      { key: 'gene_id', label: 'Gene ID', width: 100 },
      { key: 'web_id', label: 'GO ID', width: 120 },
      { key: 'tree_id', label: 'Tree ID', width: 120 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['gene_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'gene_tree_hgnc',
    label: 'Gene tree \u2014 HGNC groups',
    shortLabel: 'Genes (HGNC)',
    description:
      'Same run as "Gene tree \u2014 Gene Ontology" \u2014 this entry shows the HGNC gene-group split: a ' +
      'shallow, human-only, two-level lineage (locus_group \u2192 gene_group, e.g. protein-coding gene \u2192 ' +
      'Interleukins) sourced from HGNC\u2019s complete gene set.',
    noUpload: true,
    outputPath: 'data/gene_category_tree_hgnc.json',
    resultFormat: 'json-tree',
    workflowFile: 'extract-genes.yml',
    columns: [
      { key: 'gene_id', label: 'Gene ID', width: 100 },
      { key: 'tree_id', label: 'Group path', width: 260 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['gene_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'gene_tree_kegg',
    label: 'Gene tree \u2014 KEGG pathways',
    shortLabel: 'Genes (KEGG)',
    description:
      'Same run as "Gene tree \u2014 Gene Ontology" \u2014 this entry shows the KEGG split: genes classified ' +
      'by the KEGG BRITE pathway hierarchy (Metabolism, Human Diseases, Organismal Systems, etc.) down to ' +
      'individual pathways, via a live rest.kegg.jp lookup at run time.',
    noUpload: true,
    outputPath: 'data/gene_category_tree_kegg.json',
    resultFormat: 'json-tree',
    workflowFile: 'extract-genes.yml',
    columns: [
      { key: 'gene_id', label: 'Gene ID', width: 100 },
      { key: 'web_id', label: 'KEGG pathway', width: 130 },
      { key: 'tree_id', label: 'Pathway ID', width: 110 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['gene_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'gene_tree_type',
    label: 'Gene tree \u2014 Gene type',
    shortLabel: 'Genes (type)',
    description:
      'Same run as "Gene tree \u2014 Gene Ontology" \u2014 this entry shows the flattest split: NCBI\u2019s own ' +
      'type_of_gene field (protein-coding, ncRNA, pseudogene, etc.), one level deep, sourced from gene_info.gz.',
    noUpload: true,
    outputPath: 'data/gene_category_tree_type.json',
    resultFormat: 'json-tree',
    workflowFile: 'extract-genes.yml',
    columns: [
      { key: 'gene_id', label: 'Gene ID', width: 100 },
      { key: 'tree_id', label: 'Gene type', width: 160 },
      { key: 'count(*)', label: 'Mentions', width: 100 },
      { key: 'first', label: 'Category path', width: 480 },
    ],
    searchableColumns: ['gene_id', 'first'],
    defaultSortKey: 'count(*)',
  },
  {
    id: 'sync_public_data',
    label: 'Sync data to public/data',
    shortLabel: 'Sync public data',
    description:
      'Copies every file in data/ into public/data/ \u2014 no upload needed. The standalone dashboard HTML ' +
      'files (long_covid_dashboard_v2_enhanced*.html, gene_category_trees.html) fetch from a relative ' +
      './data/ path once deployed, so they only see whatever was in public/data/ at the last build. This ' +
      'already runs automatically before every deploy; use this entry to force a resync without doing a ' +
      'full deploy. Never deletes \u2014 files that exist only under public/data/ are left alone.',
    noUpload: true,
    outputPath: 'public/data/_sync_manifest.json',
    resultFormat: 'json-docs',
    workflowFile: 'sync-public-data.yml',
    columns: [
      { key: 'file', label: 'File', width: 320 },
      { key: 'size_bytes', label: 'Size (bytes)', width: 120 },
      { key: 'synced_at', label: 'Synced at (UTC)', width: 200 },
    ],
    searchableColumns: ['file'],
    defaultSortKey: 'file',
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
