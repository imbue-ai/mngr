// Minimal, one-way relay preload for the workspace content view.
//
// The content view hosts FOREIGN, untrusted workspace content on a separate
// origin, so it deliberately does NOT get the full `window.minds` IPC bridge
// (preload.js). This relay exposes NOTHING to the page via contextBridge; it
// only listens for an allowlisted set of `window.postMessage` types coming
// from the page and forwards them to the main process over fixed IPC channels.
// The page can therefore trigger a small set of benign shell affordances (e.g.
// opening a permission-request modal) but can never reach arbitrary IPC.
//
// The allowlist is deliberately tiny: only the affordances foreign agent content
// (and the content view's own crash page) legitimately needs. Every trusted
// local/native page (Landing, Create, Settings, workspace settings, ...) now
// renders on the CHROME surface with the full window.minds bridge, so their
// launchers (sign-in / settings / accounts / sharing / stop-mind / open-in-new /
// accent-preview) are shell-bridge calls, unreachable from -- and no longer
// relayed for -- agent content.
const { ipcRenderer } = require('electron');

// Request ids are server-issued (`evt-<uuid hex>`). Accept only a conservative
// charset + length so a malicious page cannot smuggle path or query characters
// into the `/requests/<id>` URL the main process builds. The main process
// re-validates with the same pattern (never trust the renderer).
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;

// Agent ids are server-issued (`agent-<hex>`). Accept only that conservative
// shape so a malicious page cannot smuggle path/query characters into the
// help URL the main process builds. The main process re-validates.
const AGENT_ID_PATTERN = /^agent-[a-f0-9]{1,64}$/i;

// Workspace host ids are server-issued (`host-<hex>`). Same conservative-shape
// rationale as above; the main process re-validates.
const HOST_ID_PATTERN = /^host-[a-f0-9]{1,64}$/i;

// The one outbound (main -> page) message: Cmd+W pressed while this view
// displays a workspace. Re-posted into the page as an ordinary window message
// so the system interface can close its active dockview tab. Outbound is safe
// to relay without validation -- the page can only act on itself -- and main
// only ever sends it for its own fixed reason (see registerShortcutsFor).
ipcRenderer.on('close-active-tab', () => {
  window.postMessage({ type: 'minds:close-active-tab' }, '*');
});

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
  // Error pages (the workspace-content crash page) ask the shell to open the
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
  // Crash page (crashed.html) Reload button: ask the main process to re-load the
  // workspace URL that was showing when this content view's renderer died,
  // spawning a fresh renderer. No payload -- the main process holds the pre-crash
  // URL, so a foreign page can't smuggle a navigation target through this channel.
  if (data.type === 'minds:reload-crashed-view') {
    ipcRenderer.send('reload-crashed-view');
    return;
  }
  // The workspace's Claude sign-in modal ("Sign in with Imbue") asks the shell
  // to open the desktop client's AI-key mint page for this workspace. The mint
  // page lives on the minds backend, whose origin (a random per-run port) only
  // the main process knows -- the workspace page cannot build the URL itself.
  // ``hostId`` may be empty (the workspace could not read its own host id); the
  // mint page then renders an explanation instead of the mint button. The ack
  // posted back tells the page it is running inside the desktop app; with no
  // relay (plain browser / share tunnel) no ack arrives and the modal falls
  // back to explaining that the desktop app is required.
  if (data.type === 'minds:open-ai-keys-page') {
    const hostId = data.hostId;
    if (hostId !== undefined && hostId !== '' && (typeof hostId !== 'string' || !HOST_ID_PATTERN.test(hostId))) {
      return;
    }
    ipcRenderer.send('open-ai-keys', typeof hostId === 'string' ? hostId : '');
    window.postMessage({ type: 'minds:open-ai-keys-ack' }, '*');
    return;
  }
});
