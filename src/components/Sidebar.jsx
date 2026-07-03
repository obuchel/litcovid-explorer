import { PIPELINES } from '../pipelines/registry.js';

export default function Sidebar({ activeId, onSelect }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="title">Doc Info Explorer</div>
        <div className="subtitle">LitCovid · PubTator3 · GitHub Actions</div>
      </div>

      <nav className="pipeline-nav">
        <p className="pipeline-nav-label">Pipelines</p>
        {PIPELINES.map((p) => (
          <button
            key={p.id}
            className={`pipeline-item${p.id === activeId ? ' active' : ''}`}
            onClick={() => onSelect(p.id)}
          >
            <span>{p.shortLabel}</span>
            {p.comingSoon && <span className="badge">soon</span>}
          </button>
        ))}
      </nav>

      <div className="sidebar-footer">
        Runs live on GitHub Actions, in your repo.
        <br />
        Close this tab any time — the run keeps going.
      </div>
    </aside>
  );
}
