import { useEffect, useRef, useState } from 'react';
import LogConsole from './LogConsole.jsx';
import { dispatchWorkflow, findDispatchedRun, getRun, getLatestRun } from '../lib/github.js';

/**
 * "Dashboard data" pipeline: starts build-dashboard-data.yml (slim_tags.py -> build_dashboard_data.py ->
 * add_network_pages.py, commit, redeploy) and follows the run. Save as src/components/ProcessDashboardData.jsx.
 * App.jsx renders it for the pipeline with id 'dashboard-data' instead of RunPanel, because RunPanel sends inputs
 * (limit, force_refresh, ...) that this workflow does not define and GitHub rejects unknown inputs.
 */

const WORKFLOW_FILE = 'build-dashboard-data.yml';
const POLL_INTERVAL_MS = 6000;

const STATUS_LABEL = {
  idle: null,
  dispatching: 'Starting the workflow run...',
  queued: 'Queued on GitHub Actions...',
  in_progress: 'Running on GitHub Actions...',
  completed: 'Completed',
  failed: 'Run failed',
  error: 'Error',
};

export default function ProcessDashboardData({ settings, connected }) {
  const [source, setSource] = useState('public/data/pubtator_records.jsonl.gz');
  const [meshTree, setMeshTree] = useState('public/data/mesh_category_tree_predicted_diseases.json');
  const [slimFirst, setSlimFirst] = useState(true);
  const [status, setStatus] = useState('idle');
  const [logs, setLogs] = useState([]);
  const [runInfo, setRunInfo] = useState(null);
  const pollRef = useRef(null);

  const running = ['dispatching', 'queued', 'in_progress'].includes(status);
  const busy = !connected || status === 'dispatching';
  const wf = { ...settings, workflowFile: WORKFLOW_FILE };

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
          log('success', 'Run completed. The new data is committed to public/data and the site redeploy was started.');
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

  async function handleRun() {
    if (!connected) return;
    if (!source.trim()) return log('warn', 'Enter the path of the PubTator file.');
    stopPolling();
    setRunInfo(null);
    try {
      setStatus('dispatching');
      const dispatchedAt = await dispatchWorkflow({
        ...wf,
        inputs: {
          source: source.trim(),
          slim_first: slimFirst ? 'true' : 'false',
          mesh_tree: meshTree.trim(),
        },
      });
      log('info', 'Workflow dispatched, looking for the run...');
      const run = await findDispatchedRun({ ...wf, since: dispatchedAt });
      if (!run) {
        log(
          'warn',
          'Started the run but lost track of it (GitHub API lag). Click "Check current run" — ' +
            'the run itself is unaffected and keeps going on GitHub.',
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

  async function handleCheckCurrentRun() {
    if (!connected) return;
    log('info', 'Looking for the most recent run on GitHub...');
    try {
      const run = await getLatestRun(wf);
      if (!run) return log('warn', 'No runs found for this workflow yet.');
      setRunInfo({ runId: run.id, htmlUrl: run.html_url });
      log('info', `Watching run #${run.run_number} (${run.status})...`);
      pollRun(run.id);
    } catch (err) {
      log('err', err.message);
    }
  }

  return (
    <div className="card">
      <h2><span className="step">2</span> Rebuild dashboard data</h2>
      <p style={{ margin: '0 0 12px', fontSize: 13, color: 'var(--ink-soft)' }}>
        Runs <span className="mono">slim_tags.py</span>, <span className="mono">build_dashboard_data.py</span> and{' '}
        <span className="mono">add_network_pages.py</span> on GitHub, commits the rebuilt data folder and redeploys the site.
        Nothing is uploaded: the PubTator file is read from the repo.
      </p>

      <div className="field-grid">
        <div className="field" style={{ gridColumn: 'span 2' }}>
          <label htmlFor="dd-source">PubTator file in the repo</label>
          <input id="dd-source" type="text" value={source} onChange={(e) => setSource(e.target.value)} />
        </div>
        <div className="field" style={{ gridColumn: 'span 2' }}>
          <label htmlFor="dd-mesh">MeSH category tree (optional)</label>
          <input id="dd-mesh" type="text" value={meshTree} onChange={(e) => setMeshTree(e.target.value)} />
        </div>
        <div className="field checkbox-field" style={{ alignSelf: 'end', paddingBottom: 8 }}>
          <input id="dd-slim" type="checkbox" checked={slimFirst} onChange={(e) => setSlimFirst(e.target.checked)} />
          <label htmlFor="dd-slim" style={{ margin: 0, textTransform: 'none', fontFamily: 'var(--sans)' }}>
            Run slim_tags.py first (untick if the file is already slim)
          </label>
        </div>
      </div>

      <div className="btn-row">
        <button className="btn" disabled={busy || running} onClick={handleRun}>
          {running ? 'Running...' : 'Run'}
        </button>
        <button className="btn secondary" disabled={busy} onClick={handleCheckCurrentRun}>
          Check current run
        </button>
        {running && (
          <button className="btn secondary" onClick={stopPolling}>
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
          This runs on GitHub's servers, not in your browser. You can close this tab and the run will keep going.
        </p>
      )}
    </div>
  );
}
