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
  toggleHelp: (agentId) => ipcRenderer.send('toggle-help', agentId),

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
  onCurrentWorkspaceChanged: (callback) => {
    ipcRenderer.on('current-workspace-changed', (_event, agentId) => callback(agentId));
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

  // Window controls
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),
});
