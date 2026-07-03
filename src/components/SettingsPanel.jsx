import { useState } from 'react';

export default function SettingsPanel({ settings, onChange, connected }) {
  const [showToken, setShowToken] = useState(false);

  function set(key, value) {
    onChange({ ...settings, [key]: value });
  }

  return (
    <div className="card">
      <h2><span className="step">1</span> Connect your repo</h2>
      <p style={{ margin: '0 0 4px', fontSize: 13, color: 'var(--ink-soft)' }}>
        Needs a fine-grained personal access token scoped to <em>this one repo</em> with{' '}
        <span className="mono">Contents: Read and write</span> and{' '}
        <span className="mono">Actions: Read and write</span> permissions.{' '}
        The token stays in this tab's memory only — it's never saved to disk, localStorage, or
        sent anywhere except api.github.com.
      </p>

      <div className="field-grid">
        <div className="field">
          <label htmlFor="owner">Owner</label>
          <input id="owner" value={settings.owner} onChange={(e) => set('owner', e.target.value.trim())} placeholder="dataforlibs" />
        </div>
        <div className="field">
          <label htmlFor="repo">Repository</label>
          <input id="repo" value={settings.repo} onChange={(e) => set('repo', e.target.value.trim())} placeholder="litcovid-explorer" />
        </div>
        <div className="field">
          <label htmlFor="branch">Branch</label>
          <input id="branch" value={settings.branch} onChange={(e) => set('branch', e.target.value.trim())} placeholder="main" />
        </div>
        <div className="field" style={{ gridColumn: 'span 2' }}>
          <label htmlFor="token">Personal access token</label>
          <div style={{ display: 'flex', gap: 6 }}>
            <input
              id="token"
              type={showToken ? 'text' : 'password'}
              value={settings.token}
              onChange={(e) => set('token', e.target.value.trim())}
              placeholder="github_pat_..."
              autoComplete="off"
            />
            <button type="button" className="btn secondary" onClick={() => setShowToken((s) => !s)}>
              {showToken ? 'Hide' : 'Show'}
            </button>
          </div>
        </div>
      </div>

      <div className="progress-meta" style={{ marginTop: 14 }}>
        <span>{connected ? 'Ready — repo fields and token look filled in.' : 'Fill in owner, repo, and token to continue.'}</span>
      </div>
    </div>
  );
}
