const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('minds', {
  // Platform info
  platform: process.platform,

  // Status and error callbacks (used by shell.html loading/error screen)
  onStatusUpdate: (callback) => {
    ipcRenderer.on('status-update', (_event, message) => callback(message));
  },
  onErrorDetails: (callback) => {
    ipcRenderer.on('error-details', (_event, details) => callback(details));
  },

  // Navigation
  goHome: () => ipcRenderer.send('go-home'),
  navigateContent: (url) => ipcRenderer.send('navigate-content', url),
  contentGoBack: () => ipcRenderer.send('content-go-back'),
  contentGoForward: () => ipcRenderer.send('content-go-forward'),

  // Content events (forwarded from main process)
  onContentTitleChange: (callback) => {
    ipcRenderer.on('content-title-changed', (_event, title) => callback(title));
  },
  onContentURLChange: (callback) => {
    ipcRenderer.on('content-url-changed', (_event, url) => callback(url));
  },
  onWindowTitleChange: (callback) => {
    ipcRenderer.on('window-title-changed', (_event, title) => callback(title));
  },
  onChromeEvent: (callback) => {
    ipcRenderer.on('chrome-event', (_event, data) => callback(data));
  },
  onModalStateChanged: (callback) => {
    ipcRenderer.on('modal-state-changed', (_event, data) => callback(data));
  },

  // Sidebar. The optional ``anchor`` arg is
  //   { trigger: {x, y, width, height}, offset: {x, y} }
  // (all numbers; viewport-relative). Main packs it into the sidebar's URL
  // so Sidebar.jinja can position the menu via server-rendered inline
  // style. If omitted, the server falls back to sensible defaults
  // (anchor a 38px-tall element at the top-left, nudged 2px left and 2px below it).
  toggleSidebar: (anchor) => ipcRenderer.send('toggle-sidebar', anchor),

  // Inbox modal (formerly the right-side requests panel)
  toggleInbox: () => ipcRenderer.send('toggle-inbox'),

  // Get-help modal (report a bug). ``agentId`` is the currently-displayed
  // workspace id (or '' on a general screen) so the help page can scope its
  // report to that workspace; it is packed into the help page URL by main.
  // ``assistAvailable`` marks the workspace healthy enough to host an /assist
  // chat, gating the "have an agent help" option (see chrome.js).
  toggleHelp: (agentId, assistAvailable) => ipcRenderer.send('toggle-help', agentId, assistAvailable),

  // One-shot bug report from the full-app error takeover (shell.html) when the
  // backend is down and the normal /help flow is unreachable. Reports the
  // on-screen error via the main-process Sentry. ``includeLogs`` is the
  // takeover's per-report "Include recent logs" opt-in (the persistent
  // include-logs setting is OR'd in by main). Resolves to ``{ ok, eventId }``
  // so the shell can show the copyable report id.
  reportError: (includeLogs) => ipcRenderer.invoke('report-error', { includeLogs }),

  // Whether the persistent ``include_error_logs`` setting is on, so the takeover
  // can decide whether to offer its per-report "Include recent logs" checkbox
  // (shown only when the setting is off; when on, logs are always attached).
  getLogInclusionSetting: () => ipcRenderer.invoke('get-log-inclusion-setting'),

  // Modal overlay close (used by the inbox shell and any one-off dialogs)
  closeModal: () => ipcRenderer.send('close-modal'),

  // Overlay surface (the always-warm modal WebContentsView host page,
  // /_chrome/overlay). The overlay manager (/_static/overlay.js) receives
  // show/hide commands from main via ``onOverlayCommand`` and reports the
  // overlay view's required bounds back via ``overlaySetBounds`` so main can
  // size the view (Electron has no per-view click-through; bounds are the only
  // lever). ``spec`` is { mode: 'hidden' } |
  // { mode: 'rect', rect: {x, y, width, height} }.
  onOverlayCommand: (callback) => {
    ipcRenderer.on('overlay-command', (_event, cmd) => callback(cmd));
  },
  overlaySetBounds: (spec) => ipcRenderer.send('overlay-set-bounds', spec),
  // Fired by the overlay host once a hosted modal iframe has loaded, so main can
  // replay the cached chrome state into that frame (the sidebar's workspace list,
  // the inbox's request count) without waiting for the next SSE push.
  overlayModalLoaded: (id) => ipcRenderer.send('overlay-modal-loaded', id),

  // Custom titlebar tooltips. The chrome view computes a trigger's
  // viewport-relative rect and its label, and main forwards it to the overlay
  // host to render above both chrome and content. ``payload`` is
  // { rect: {x, y, width, height}, text, shortcut?, html? }.
  showTooltip: (payload) => ipcRenderer.send('show-tooltip', payload),
  hideTooltip: () => ipcRenderer.send('hide-tooltip'),

  // Native file/directory picker used by the file-sharing permission
  // dialog so the user can pick the path to share instead of typing it.
  // ``options.mode`` is 'file' or 'directory'; ``options.defaultPath``
  // seeds the dialog's starting location. Resolves to the selected
  // absolute path, or null if the user cancelled.
  showFilePicker: (options) => ipcRenderer.invoke('show-file-picker', options),

  // Multi-window workspace actions
  openWorkspaceInNewWindow: (agentId) =>
    ipcRenderer.send('open-workspace-in-new-window', agentId),
  navigateToRequest: (agentId, eventId) =>
    ipcRenderer.send('navigate-to-request', agentId, eventId),
  showWorkspaceContextMenu: (agentId, x, y) =>
    ipcRenderer.send('show-workspace-context-menu', agentId, x, y),
  // ``contentReady`` is whether the content view is showing a reachable
  // workspace (vs the mngr_forward "Loading workspace" 503 loader), so the
  // titlebar can keep the "have an agent help" option disabled while loading.
  onCurrentWorkspaceChanged: (callback) => {
    ipcRenderer.on('current-workspace-changed', (_event, agentId, contentReady) => callback(agentId, contentReady));
  },

  // The accent source for THIS window's current screen: the workspace id on
  // a workspace-scoped screen (the workspace itself plus its settings /
  // sharing / destroying / recovery screens) and null on a general screen.
  // Main pushes it on every navigation (and on workspace-delete / sign-out),
  // and re-pushes the current value when the chrome view (re)loads, so the
  // titlebar paints the right accent -- or the neutral chrome -- without the
  // renderer remembering anything.
  onAccentChanged: (callback) => {
    ipcRenderer.on('accent-changed', (_event, agentId) => callback(agentId));
  },

  // Actions
  retry: () => ipcRenderer.send('retry'),
  openLogFile: () => ipcRenderer.send('open-log-file'),
  // Reload the chrome (titlebar) view after its renderer crashed -- the Reload
  // button on the local chrome-crashed.html strip.
  reloadChrome: () => ipcRenderer.send('reload-chrome'),

  // Window controls
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),
});
