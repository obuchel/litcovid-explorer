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
