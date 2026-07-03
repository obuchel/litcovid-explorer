import DataTable from './DataTable.jsx';
import { downloadText } from '../lib/download.js';

export default function ResultsPanel({ pipeline, rows, csvText, filename }) {
  const failedCount = rows.filter((r) => r.fetch_status === 'failed').length;

  return (
    <div className="card">
      <h2><span className="step">3</span> Explore results</h2>

      <div className="stat-row">
        <div className="stat">
          <div className="value">{rows.length.toLocaleString()}</div>
          <div className="label">Rows</div>
        </div>
        <div className="stat">
          <div className="value">{(rows.length - failedCount).toLocaleString()}</div>
          <div className="label">Fetched OK</div>
        </div>
        <div className="stat">
          <div className="value">{failedCount.toLocaleString()}</div>
          <div className="label">Failed</div>
        </div>
      </div>

      <div className="btn-row">
        <button className="btn" disabled={!csvText} onClick={() => downloadText(filename, csvText)}>
          Download {filename}
        </button>
      </div>

      <div style={{ marginTop: 20 }}>
        <DataTable
          rows={rows}
          columns={pipeline.columns}
          searchableColumns={pipeline.searchableColumns}
          defaultSortKey={pipeline.defaultSortKey}
        />
      </div>
    </div>
  );
}
