// Minimal, one-way relay preload for the workspace content view.
//
// The content view hosts FOREIGN, untrusted workspace content on a separate
// origin, so it deliberately does NOT get the full `window.minds` IPC bridge
// (preload.js). This relay exposes NOTHING to the page via contextBridge; it
// only listens for an allowlisted set of `window.postMessage` types coming
// from the page and forwards them to the main process over fixed IPC channels.
// The page can therefore trigger a small set of benign shell affordances (e.g.
// opening a permission-request modal) but can never reach arbitrary IPC.
const { ipcRenderer } = require('electron');

// Request ids are server-issued (`evt-<uuid hex>`). Accept only a conservative
// charset + length so a malicious page cannot smuggle path or query characters
// into the `/requests/<id>` URL the main process builds. The main process
// re-validates with the same pattern (never trust the renderer).
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

// Agent ids are server-issued (`agent-<hex>`). Accept only that conservative
// shape so a malicious page cannot smuggle path/query characters into the
// stop-host URL the main process builds. The main process re-validates.
const AGENT_ID_PATTERN = /^agent-[a-f0-9]{1,64}$/i;

// #rrggbb lowercase hex (the canonical form ``normalize_workspace_color``
// emits). Accepts only the strict shape so a malicious page can't paint
// the titlebar with arbitrary CSS values via the preview channel.
const ACCENT_HEX_PATTERN = /^#[0-9a-f]{6}$/;

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
  // Error pages (e.g. the workspace-recovery page) ask the shell to open the
  // get-help / report-a-bug modal. ``agentId`` is optional -- when present it
  // scopes the report to that workspace; it is validated to the server-issued
  // shape (or accepted as empty) so a foreign page can't smuggle path/query
  // characters into the help URL the main process builds.
  if (data.type === 'minds:open-help') {
    const agentId = data.agentId;
    if (agentId !== undefined && agentId !== '' && (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId))) {
      return;
    }
    ipcRenderer.send('open-help', typeof agentId === 'string' ? agentId : '');
    return;
  }
  // Create-screen sign-in: open the shared modal overlay loaded with the
  // sign-in page (so it covers the whole window, including the title bar).
  // No payload -- the main process builds the fixed `/auth/signin-modal` URL.
  if (data.type === 'minds:open-signin-modal') {
    ipcRenderer.send('open-signin-modal');
    return;
  }
  // Landing-page Stop button: ask the main process to show a native
  // confirmation dialog and (on confirm) issue the host stop itself.
  if (data.type === 'minds:confirm-stop-mind') {
    const agentId = data.agentId;
    if (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId)) return;
    const name = typeof data.name === 'string' ? data.name : agentId;
    ipcRenderer.send('confirm-stop-mind', agentId, name);
    return;
  }
  // Landing-page "open in new window" button: ask the main process to open
  // (or focus) a dedicated window for this workspace. Same IPC channel the
  // sidebar uses; the agent id is validated to the server-issued shape so a
  // foreign page can't smuggle path/query chars into the URL main builds.
  if (data.type === 'minds:open-workspace-in-new-window') {
    const agentId = data.agentId;
    if (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId)) return;
    ipcRenderer.send('open-workspace-in-new-window', agentId);
    return;
  }
  // Settings-page color picker: paint the chrome titlebar optimistically
  // *in this bundle's chrome view* so the user sees the picked color
  // immediately, without waiting for the POST -> mngr label subprocess
  // -> SSE round-trip. The actual persistence still goes through the
  // POST endpoint; this just shortcuts the local-window UI feedback.
  // Validated narrowly (agent id shape, #rrggbb lowercase hex, fixed
  // foreground triples) so a foreign workspace page can't smuggle
  // arbitrary CSS through.
  if (data.type === 'minds:preview-workspace-accent') {
    const agentId = data.agentId;
    const accent = data.accent;
    if (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId)) return;
    if (typeof accent !== 'string' || !ACCENT_HEX_PATTERN.test(accent)) return;
    ipcRenderer.send('preview-workspace-accent', agentId, accent);
    return;
  }
});
