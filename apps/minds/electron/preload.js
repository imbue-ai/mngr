const { contextBridge, ipcRenderer } = require('electron');

// Handler registered by the renderer (e.g. the Dockview workspace) to close
// its "active tab". The main process sends 'close-active-tab-request' with a
// correlation id and awaits 'close-active-tab-response' on that same id.
// Responses carry a boolean: true if a tab was closed, false otherwise --
// main uses that to decide whether to fall back to closing the window.
let closeActiveTabHandler = null;
ipcRenderer.on('close-active-tab-request', async (_event, requestId) => {
  let closed = false;
  try {
    if (typeof closeActiveTabHandler === 'function') {
      closed = !!(await closeActiveTabHandler());
    }
  } catch {
    closed = false;
  }
  ipcRenderer.send('close-active-tab-response', requestId, closed);
});

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

  // Close-active-tab hook: the renderer (e.g. DockviewWorkspace) registers
  // a handler invoked when the user presses cmd+w. The handler should close
  // the currently focused tab if one exists and return true, else false.
  setCloseActiveTabHandler: (handler) => {
    closeActiveTabHandler = typeof handler === 'function' ? handler : null;
  },
});
