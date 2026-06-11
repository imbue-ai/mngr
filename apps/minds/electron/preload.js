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

  // Persisted "last opened workspace" agent id. The chrome page reads this
  // on bootstrap to paint the titlebar accent before the first
  // ``current-workspace-changed`` event arrives. Main owns writes
  // (driven by ``current-workspace-changed`` + SSE-driven cleanup) and
  // broadcasts updates via ``onLastWorkspaceAgentIdChanged`` (workspace
  // deleted, user signed out, a different bundle opened a workspace).
  getLastWorkspaceAgentId: () => ipcRenderer.invoke('get-last-workspace-agent-id'),
  onLastWorkspaceAgentIdChanged: (callback) => {
    ipcRenderer.on('last-workspace-agent-id-changed', (_event, agentId) => callback(agentId));
  },

  // Actions
  retry: () => ipcRenderer.send('retry'),
  openLogFile: () => ipcRenderer.send('open-log-file'),

  // Window controls
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),
});
