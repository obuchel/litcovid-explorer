// All calls go straight from the browser to api.github.com using a token the
// user pastes into the app. The token is kept only in React state for this
// tab's session (see App.jsx) — it is never written to localStorage,
// sessionStorage, or any file, and never sent anywhere except api.github.com.

const API = 'https://api.github.com';

function headers(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };
}

async function apiFetch(url, token, options = {}) {
  const res = await fetch(url, { ...options, headers: { ...headers(token), ...(options.headers || {}) } });
  if (!res.ok) {
    let detail = '';
    try {
      detail = (await res.json()).message;
    } catch {
      /* ignore */
    }
    throw new Error(`GitHub API ${res.status} on ${url}${detail ? `: ${detail}` : ''}`);
  }
  return res.status === 204 ? null : res.json();
}

function b64EncodeUnicode(str) {
  const bytes = new TextEncoder().encode(str);
  let binary = '';
  bytes.forEach((b) => { binary += String.fromCharCode(b); });
  return btoa(binary);
}

/** Fetch a file's current sha (needed to update it) and decoded text content, or null if it doesn't exist yet. */
export async function getRepoFile({ token, owner, repo, branch, path }) {
  try {
    const data = await apiFetch(
      `${API}/repos/${owner}/${repo}/contents/${encodeURIComponent(path)}?ref=${branch}`,
      token,
    );
    const binary = atob(data.content.replace(/\n/g, ''));
    const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
    const text = new TextDecoder('utf-8').decode(bytes);
    return { sha: data.sha, text };
  } catch (err) {
    if (String(err.message).includes('404')) return null;
    throw err;
  }
}

/** Create or update a file in the repo. */
export async function putRepoFile({ token, owner, repo, branch, path, content, message }) {
  const existing = await getRepoFile({ token, owner, repo, branch, path });
  return apiFetch(`${API}/repos/${owner}/${repo}/contents/${encodeURIComponent(path)}`, token, {
    method: 'PUT',
    body: JSON.stringify({
      message,
      content: b64EncodeUnicode(content),
      branch,
      ...(existing ? { sha: existing.sha } : {}),
    }),
  });
}

/** Trigger a workflow_dispatch run. workflowFile is e.g. "fetch-doc-info.yml". */
export async function dispatchWorkflow({ token, owner, repo, branch, workflowFile, inputs = {} }) {
  const dispatchedAt = new Date();
  await apiFetch(`${API}/repos/${owner}/${repo}/actions/workflows/${workflowFile}/dispatches`, token, {
    method: 'POST',
    body: JSON.stringify({ ref: branch, inputs }),
  });
  return dispatchedAt;
}

/** Find the run that was just dispatched (GitHub doesn't return a run id from the dispatch call itself). */
export async function findDispatchedRun({ token, owner, repo, workflowFile, since, timeoutMs = 45000 }) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const data = await apiFetch(
      `${API}/repos/${owner}/${repo}/actions/workflows/${workflowFile}/runs?event=workflow_dispatch&per_page=5`,
      token,
    );
    const run = (data.workflow_runs || []).find((r) => new Date(r.created_at) >= new Date(since.getTime() - 10000));
    if (run) return run;
    await sleep(2500);
  }
  return null;
}

export async function getRun({ token, owner, repo, runId }) {
  return apiFetch(`${API}/repos/${owner}/${repo}/actions/runs/${runId}`, token);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
