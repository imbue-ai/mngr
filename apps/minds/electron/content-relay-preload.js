// Minimal, one-way relay preload for the workspace content view.
//
// The content view hosts FOREIGN, untrusted workspace content on a separate
// origin, so it deliberately does NOT get the full `window.minds` IPC bridge
// (preload.js). This relay exposes NOTHING to the page via contextBridge; it
// only listens for an allowlisted set of `window.postMessage` types coming
// from the page and forwards them to the main process over fixed IPC channels.
// The page can therefore trigger a small set of benign shell affordances (e.g.
// opening a permission-request modal, asking the chrome to open a workspace
// in a new window from the landing page) but can never reach arbitrary IPC.
const { ipcRenderer } = require('electron');

// Request ids are server-issued (`evt-<uuid hex>`). Accept only a conservative
// charset + length so a malicious page cannot smuggle path or query characters
// into the `/requests/<id>` URL the main process builds. The main process
// re-validates with the same pattern (never trust the renderer).
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

// Agent ids are validated server-side (see imbue.imbue_common ids). Use the
// same conservative charset here so a malicious page cannot smuggle path or
// query characters into the chrome's IPC. The main process re-validates.
const AGENT_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

function isFiniteNumber(n) {
  return typeof n === 'number' && Number.isFinite(n);
}

window.addEventListener('message', (event) => {
  // Only honour messages posted by this same top-level page, never by a
  // nested third-party iframe the workspace might embed.
  if (event.source !== window) return;
  const data = event.data;
  if (!data || typeof data !== 'object') return;
  if (data.type === 'minds:open-request-modal') {
    const requestId = data.requestId;
    if (typeof requestId !== 'string' || !REQUEST_ID_PATTERN.test(requestId)) return;
    ipcRenderer.send('open-request-modal', requestId);
    return;
  }
  if (data.type === 'minds:open-workspace-new-window') {
    const agentId = data.agentId;
    if (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId)) return;
    ipcRenderer.send('open-workspace-in-new-window', agentId);
    return;
  }
  if (data.type === 'minds:show-workspace-context-menu') {
    const agentId = data.agentId;
    if (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId)) return;
    const x = isFiniteNumber(data.x) ? data.x : 0;
    const y = isFiniteNumber(data.y) ? data.y : 0;
    ipcRenderer.send('show-workspace-context-menu', agentId, x, y);
    return;
  }
});
