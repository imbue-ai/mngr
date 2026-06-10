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
// Either "0 0 0" or "255 255 255" (the only two values
// ``pick_workspace_foreground`` ever returns).
const ACCENT_FG_PATTERN = /^(?:0 0 0|255 255 255)$/;

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
  // Landing-page Stop button: ask the main process to show a native
  // confirmation dialog and (on confirm) issue the host stop itself.
  if (data.type === 'minds:confirm-stop-mind') {
    const agentId = data.agentId;
    if (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId)) return;
    const name = typeof data.name === 'string' ? data.name : agentId;
    ipcRenderer.send('confirm-stop-mind', agentId, name);
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
    const accentFg = data.accentFg;
    if (typeof agentId !== 'string' || !AGENT_ID_PATTERN.test(agentId)) return;
    if (typeof accent !== 'string' || !ACCENT_HEX_PATTERN.test(accent)) return;
    if (typeof accentFg !== 'string' || !ACCENT_FG_PATTERN.test(accentFg)) return;
    ipcRenderer.send('preview-workspace-accent', agentId, accent, accentFg);
    return;
  }
  // Create-form color picker: there's no workspace yet, so we can't
  // route through the per-agent cache. This path paints the chrome
  // CSS variables directly for the duration of the create flow; a
  // subsequent navigation event repaints from whatever the new
  // displayed/last workspace is.
  if (data.type === 'minds:preview-freeform-accent') {
    const accent = data.accent;
    const accentFg = data.accentFg;
    if (typeof accent !== 'string' || !ACCENT_HEX_PATTERN.test(accent)) return;
    if (typeof accentFg !== 'string' || !ACCENT_FG_PATTERN.test(accentFg)) return;
    ipcRenderer.send('preview-freeform-accent', accent, accentFg);
    return;
  }
});
