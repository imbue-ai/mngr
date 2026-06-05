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

  // Requests inbox modal. The legacy channel name 'toggle-requests-panel'
  // is kept on the wire to avoid an unrelated preload bump; the main-
  // process handler now opens the inbox modal instead.
  toggleRequestsPanel: () => ipcRenderer.send('toggle-requests-panel'),

  // Modal overlay (e.g. permission request dialogs)
  closeModal: () => ipcRenderer.send('close-modal'),

  // Multi-window workspace actions
  openWorkspaceInNewWindow: (agentId) =>
    ipcRenderer.send('open-workspace-in-new-window', agentId),
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
});
