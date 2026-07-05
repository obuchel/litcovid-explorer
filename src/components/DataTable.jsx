import { useMemo, useState } from 'react';

const PAGE_SIZE = 25;

export default function DataTable({ rows, columns, searchableColumns, defaultSortKey }) {
  const [query, setQuery] = useState('');
  const [sortKey, setSortKey] = useState(defaultSortKey);
  const [sortDir, setSortDir] = useState('asc');
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState(null);

  const filtered = useMemo(() => {
    if (!query.trim()) return rows;
    const q = query.trim().toLowerCase();
    const cols = searchableColumns?.length ? searchableColumns : columns.map((c) => c.key);
    return rows.filter((row) => cols.some((key) => String(row[key] ?? '').toLowerCase().includes(q)));
  }, [rows, query, columns, searchableColumns]);

  const sorted = useMemo(() => {
    if (!sortKey) return filtered;
    const copy = [...filtered];
    copy.sort((a, b) => {
      const av = String(a[sortKey] ?? '');
      const bv = String(b[sortKey] ?? '');
      const cmp = av.localeCompare(bv, undefined, { numeric: true });
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return copy;
  }, [filtered, sortKey, sortDir]);

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages - 1);
  const pageRows = sorted.slice(currentPage * PAGE_SIZE, currentPage * PAGE_SIZE + PAGE_SIZE);

  function toggleSort(key) {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'));
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
    setPage(0);
  }

  if (!rows.length) {
    return <div className="empty-state">Run the pipeline to populate this table.</div>;
  }

  return (
    <div>
      <div className="table-toolbar">
        <input
          type="search"
          placeholder={`Search ${sorted.length} rows...`}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setPage(0);
          }}
        />
        <span className="mono" style={{ fontSize: 12, color: 'var(--ink-soft)' }}>
          {sorted.length} of {rows.length} rows
        </span>
      </div>

      <div className="table-scroll">
        <table className="data-table">
          <thead>
            <tr>
              {columns.map((col) => (
                <th key={col.key} onClick={() => toggleSort(col.key)} style={{ minWidth: col.width }}>
                  {col.label}
                  {sortKey === col.key && <span className="arrow">{sortDir === 'asc' ? '↑' : '↓'}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row, i) => {
              const rowKey = row.pmid ?? `${currentPage}-${i}`;
              const isExpanded = expanded === rowKey;
              return (
                <tr key={rowKey} onClick={() => setExpanded(isExpanded ? null : rowKey)} style={{ cursor: 'pointer' }}>
                  {columns.map((col) => (
                    <td key={col.key}>
                      <Cell columnKey={col.key} value={row[col.key]} expanded={isExpanded} />
                    </td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="table-footer">
        <span>
          Page {currentPage + 1} of {totalPages}
        </span>
        <div className="pager">
          <button disabled={currentPage === 0} onClick={() => setPage(0)}>«</button>
          <button disabled={currentPage === 0} onClick={() => setPage((p) => p - 1)}>‹</button>
          <button disabled={currentPage >= totalPages - 1} onClick={() => setPage((p) => p + 1)}>›</button>
          <button disabled={currentPage >= totalPages - 1} onClick={() => setPage(totalPages - 1)}>»</button>
        </div>
      </div>
    </div>
  );
}

function Cell({ columnKey, value, expanded }) {
  if (columnKey === 'fetch_status') {
    const ok = value === 'ok';
    return <span className={`status-pill ${ok ? 'ok' : 'failed'}`}>{value || '—'}</span>;
  }
  if (columnKey === 'doi' && value) {
    return (
      <a href={`https://doi.org/${value}`} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()}>
        {value}
      </a>
    );
  }
  if (columnKey === 'pmid' && value) {
    return (
      <a
        href={`https://pubmed.ncbi.nlm.nih.gov/${value}/`}
        target="_blank"
        rel="noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="mono"
      >
        {value}
      </a>
    );
  }
  if (columnKey === 'title_e' || columnKey === 'abstract' || columnKey === 'authors' || columnKey === 'first') {
    return <div className={expanded ? '' : 'cell-truncate'}>{value || '—'}</div>;
  }
  return <>{value || '—'}</>;
}
