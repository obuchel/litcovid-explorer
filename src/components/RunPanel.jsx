import FileDropzone from './FileDropzone.jsx';
import LogConsole from './LogConsole.jsx';

const STATUS_LABEL = {
  idle: null,
  committing: 'Committing file to the repo...',
  dispatching: 'Starting the workflow run...',
  queued: 'Queued on GitHub Actions...',
  in_progress: 'Running on GitHub Actions...',
  completed: 'Completed',
  failed: 'Run failed',
  error: 'Error',
};

export default function RunPanel({
  pipeline,
  file,
  onFile,
  options,
  onOptionsChange,
  status,
  logs,
  runInfo,
  onCommitAndRun,
  onLoadLatest,
  onCheckCurrentRun,
  onStopWatching,
  busy,
}) {
  const running = ['committing', 'dispatching', 'queued', 'in_progress'].includes(status);
  const canRun = pipeline.noUpload ? !busy : Boolean(file) && !busy;

  return (
    <div className="card">
      <h2><span className="step">2</span> {pipeline.noUpload ? 'Run' : 'Upload & run'}</h2>

      {!pipeline.noUpload && (
        <FileDropzone
          accept={pipeline.acceptedFileTypes}
          hint={pipeline.inputHint}
          file={file}
          onFile={onFile}
          disabled={busy}
        />
      )}

      <div className="field-grid">
        <div className="field">
          <label htmlFor="limit">Limit (test run)</label>
          <input
            id="limit"
            type="number"
            min="1"
            placeholder="all PMIDs"
            value={options.limit}
            onChange={(e) => onOptionsChange({ ...options, limit: e.target.value })}
          />
        </div>
        {(pipeline.id === 'litcovid_docs' || pipeline.id === 'mesh_subjects') && (
          <div className="field checkbox-field" style={{ alignSelf: 'end', paddingBottom: 8 }}>
            <input
              id="force"
              type="checkbox"
              checked={options.forceRefresh}
              onChange={(e) => onOptionsChange({ ...options, forceRefresh: e.target.checked })}
            />
            <label htmlFor="force" style={{ margin: 0, textTransform: 'none', fontFamily: 'var(--sans)' }}>
              {pipeline.id === 'mesh_subjects' ? 'Bypass the PubTator cache (re-fetch fresh annotations)' : 'Re-fetch cached PMIDs'}
            </label>
          </div>
        )}
        {pipeline.id === 'mesh_tree' && (
          <div className="field checkbox-field" style={{ alignSelf: 'end', paddingBottom: 8 }}>
            <input
              id="skip-enrichment"
              type="checkbox"
              checked={options.skipEnrichment}
              onChange={(e) => onOptionsChange({ ...options, skipEnrichment: e.target.checked })}
            />
            <label htmlFor="skip-enrichment" style={{ margin: 0, textTransform: 'none', fontFamily: 'var(--sans)' }}>
              Skip citation enrichment (iCite/OpenAlex), just rebuild the tree
            </label>
          </div>
        )}
        {pipeline.id === 'mesh_subjects' && (
          <div className="field" style={{ gridColumn: 'span 2' }}>
            <label htmlFor="pmids">Target specific PMIDs (optional)</label>
            <input
              id="pmids"
              type="text"
              placeholder="e.g. 42070008,42069416 — leave blank to scan for empty subjects"
              value={options.pmids || ''}
              onChange={(e) => onOptionsChange({ ...options, pmids: e.target.value })}
            />
          </div>
        )}
      </div>

      <div className="btn-row">
        <button className="btn" disabled={!canRun} onClick={onCommitAndRun}>
          {running ? 'Running...' : pipeline.noUpload ? 'Run' : 'Commit & run'}
        </button>
        <button className="btn secondary" disabled={busy} onClick={onLoadLatest}>
          Load latest results
        </button>
        <button className="btn secondary" disabled={busy} onClick={onCheckCurrentRun}>
          Check current run
        </button>
        {running && (
          <button className="btn secondary" onClick={onStopWatching}>
            Stop watching (run keeps going on GitHub)
          </button>
        )}
        {runInfo?.htmlUrl && (
          <a href={runInfo.htmlUrl} target="_blank" rel="noreferrer" style={{ fontSize: 12.5 }}>
            View run on GitHub ↗
          </a>
        )}
      </div>

      {status !== 'idle' && (
        <div className="progress-meta">
          <span>{STATUS_LABEL[status]}</span>
        </div>
      )}

      <LogConsole lines={logs} />

      {running && (
        <p style={{ fontSize: 12.5, color: 'var(--ink-soft)', marginTop: 12, marginBottom: 0 }}>
          This runs on GitHub's servers, not in your browser — you can close this tab and the run
          will keep going. Come back later and click "Load latest results."
        </p>
      )}
    </div>
  );
}
