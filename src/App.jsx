import { useEffect, useRef, useState } from 'react';
import Papa from 'papaparse';
import Sidebar from './components/Sidebar.jsx';
import SettingsPanel from './components/SettingsPanel.jsx';
import RunPanel from './components/RunPanel.jsx';
import ResultsPanel from './components/ResultsPanel.jsx';
import { PIPELINES, getPipeline } from './pipelines/registry.js';
import { getRepoFile, putRepoFile, dispatchWorkflow, findDispatchedRun, getRun, getLatestRun } from './lib/github.js';

const POLL_INTERVAL_MS = 6000;

export default function App() {
  const [activeId, setActiveId] = useState(PIPELINES[0].id);
  const pipeline = getPipeline(activeId);

  const [settings, setSettings] = useState({ owner: '', repo: '', branch: 'main', token: '' });
  const connected = Boolean(settings.owner && settings.repo && settings.branch && settings.token);

  const [file, setFile] = useState(null);
  const [options, setOptions] = useState({ limit: '', forceRefresh: false, skipEnrichment: false, pmids: '', dryRun: false });
  const [status, setStatus] = useState('idle');
  const [logs, setLogs] = useState([]);
  const [runInfo, setRunInfo] = useState(null);
  const [rows, setRows] = useState([]);
  const [resultText, setResultText] = useState('');
  const pollRef = useRef(null);

  // Reset the run/results view whenever the selected pipeline changes.
  useEffect(() => {
    setFile(null);
    setStatus('idle');
    setLogs([]);
    setRunInfo(null);
    setRows([]);
    setResultText('');
    stopPolling();
  }, [activeId]);

  useEffect(() => () => stopPolling(), []);

  function log(level, message) {
    setLogs((prev) => [...prev, { level, message }]);
  }

  function stopPolling() {
    if (pollRef.current) {
      clearTimeout(pollRef.current);
      pollRef.current = null;
    }
  }

  function loadResultIntoTable(text) {
    if (pipeline.resultFormat === 'json-tree') {
      const parsed = JSON.parse(text);
      setRows(parsed.tree || []);
    } else if (pipeline.resultFormat === 'json-docs') {
      const parsed = JSON.parse(text);
      setRows(parsed.docs || []);
    } else {
      const parsed = Papa.parse(text, { header: true, skipEmptyLines: true });
      setRows(parsed.data);
    }
    setResultText(text);
  }

  async function handleCheckCurrentRun() {
    if (!connected || !pipeline.workflowFile) return;
    log('info', 'Looking for the most recent run on GitHub...');
    try {
      const run = await getLatestRun({ ...settings, workflowFile: pipeline.workflowFile });
      if (!run) {
        log('warn', 'No runs found for this workflow yet.');
        return;
      }
      setRunInfo({ runId: run.id, htmlUrl: run.html_url });
      log('info', `Watching run #${run.run_number} (${run.status})...`);
      pollRun(run.id);
    } catch (err) {
      log('err', err.message);
    }
  }

  async function handleLoadLatest() {
    if (!connected) return log('warn', 'Fill in the repo settings first.');
    setStatus('idle');
    log('info', `Fetching ${pipeline.outputPath} from the repo...`);
    try {
      const file = await getRepoFile({ ...settings, path: pipeline.outputPath });
      if (!file) {
        log('warn', `${pipeline.outputPath} doesn't exist in the repo yet — run the pipeline first.`);
        return;
      }
      loadResultIntoTable(file.text);
      log('success', `Loaded ${pipeline.outputPath}.`);
    } catch (err) {
      log('err', err.message);
    }
  }

  async function handleCommitAndRun() {
    if (!connected || !pipeline.workflowFile) return;
    if (!pipeline.noUpload && !file) return;
    stopPolling();
    setRunInfo(null);
    setRows([]);
    setResultText('');

    try {
      if (!pipeline.noUpload) {
        setStatus('committing');
        log('info', `Reading ${file.name}...`);
        const text = await file.text();

        log('info', `Committing to ${pipeline.inputPath}...`);
        await putRepoFile({
          ...settings,
          path: pipeline.inputPath,
          content: text,
          message: `Update ${pipeline.inputPath} via web uploader`,
        });
        log('success', 'File committed.');
      }

      setStatus('dispatching');
      const inputs = {};
      if (options.limit) inputs.limit = String(options.limit);
      if (options.forceRefresh) inputs.force_refresh = 'true';
      if (options.skipEnrichment) inputs.skip_citation_enrichment = 'true';
      if (options.pmids && options.pmids.trim()) inputs.pmids = options.pmids.trim();
      if (options.dryRun) inputs.dry_run = 'true';

      const dispatchedAt = await dispatchWorkflow({
        ...settings,
        workflowFile: pipeline.workflowFile,
        inputs,
      });
      log('info', 'Workflow dispatched, looking for the run...');

      const run = await findDispatchedRun({
        ...settings,
        workflowFile: pipeline.workflowFile,
        since: dispatchedAt,
      });
      if (!run) {
        log(
          'warn',
          'Started the run but lost track of it (GitHub API lag). Click "Check current run" below — ' +
            'the run itself is unaffected and keeps going on GitHub regardless.',
        );
        setStatus('idle');
        return;
      }
      setRunInfo({ runId: run.id, htmlUrl: run.html_url });
      log('info', `Watching run #${run.run_number}...`);
      pollRun(run.id);
    } catch (err) {
      setStatus('error');
      log('err', err.message);
    }
  }

  function pollRun(runId) {
    setStatus('queued');
    const tick = async () => {
      try {
        const run = await getRun({ ...settings, runId });
        if (run.status !== 'completed') {
          setStatus(run.status === 'queued' ? 'queued' : 'in_progress');
          pollRef.current = setTimeout(tick, POLL_INTERVAL_MS);
          return;
        }
        if (run.conclusion === 'success') {
          setStatus('completed');
          log('success', 'Run completed. Fetching results...');
          const out = await getRepoFile({ ...settings, path: pipeline.outputPath });
          if (out) {
            loadResultIntoTable(out.text);
            log('success', `Loaded ${pipeline.outputPath}.`);
          } else {
            log('warn', `Run succeeded but ${pipeline.outputPath} wasn't found — check the workflow.`);
          }
        } else {
          setStatus('failed');
          log('err', `Run finished with conclusion "${run.conclusion}". Open the run on GitHub for logs.`);
        }
      } catch (err) {
        setStatus('error');
        log('err', err.message);
      }
    };
    tick();
  }

  return (
    <div className="shell">
      <Sidebar activeId={activeId} onSelect={setActiveId} />

      <main className="main">
        <div className="page-header">
          <h1>{pipeline.label}</h1>
          <p>{pipeline.description}</p>
        </div>

        {pipeline.comingSoon ? (
          <div className="card" style={{ marginTop: 20 }}>
            <div className="coming-soon">
              This pipeline is a template, not a finished tool yet. Implement <code>scripts/&lt;name&gt;.py</code>,
              add a workflow under <code>.github/workflows/</code>, and fill in this pipeline's entry in{' '}
              <code>src/pipelines/registry.js</code> — the app picks it up automatically. See README.md → "Adding
              a pipeline."
            </div>
          </div>
        ) : (
          <>
            <SettingsPanel settings={settings} onChange={setSettings} connected={connected} />

            <RunPanel
              pipeline={pipeline}
              file={file}
              onFile={setFile}
              options={options}
              onOptionsChange={setOptions}
              status={status}
              logs={logs}
              runInfo={runInfo}
              onCommitAndRun={handleCommitAndRun}
              onLoadLatest={handleLoadLatest}
              onCheckCurrentRun={handleCheckCurrentRun}
              onStopWatching={stopPolling}
              busy={!connected || ['committing', 'dispatching'].includes(status)}
            />

            {rows.length > 0 && (
              <ResultsPanel pipeline={pipeline} rows={rows} resultText={resultText} filename={pipeline.outputPath.split('/').pop()} />
            )}
          </>
        )}
      </main>
    </div>
  );
}
