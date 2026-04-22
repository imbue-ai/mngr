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

  // Sidebar
  toggleSidebar: () => ipcRenderer.send('toggle-sidebar'),

  // Requests panel
  toggleRequestsPanel: () => ipcRenderer.send('toggle-requests-panel'),
  openRequestsPanel: () => ipcRenderer.send('open-requests-panel'),

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

  // Actions
  retry: () => ipcRenderer.send('retry'),
  openLogFile: () => ipcRenderer.send('open-log-file'),

  // Window controls
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),

  // Lima lazy install (called by the create form when LIMA mode is
  // selected, so the tarball downloads in the background while the user
  // fills out the form).
  ensureLima: () => ipcRenderer.invoke('ensure-lima'),
  isLimaAvailable: () => ipcRenderer.invoke('is-lima-available'),
  onLimaProgress: (callback) => {
    ipcRenderer.on('lima-progress', (_event, pct) => callback(pct));
  },

  // Non-blocking auto-update: chrome titlebar shows an "Update" button when
  // ToDesktop reports an update has finished downloading. Clicking it
  // applies the update and restarts. See main.js for the wiring.
  isUpdateReady: () => ipcRenderer.invoke('is-update-ready'),
  onUpdateReady: (callback) => {
    ipcRenderer.on('update-ready', () => callback());
  },
  installUpdate: () => ipcRenderer.send('install-update'),
});
