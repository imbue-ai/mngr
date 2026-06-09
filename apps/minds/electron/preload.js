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

  // Sidebar
  toggleSidebar: () => ipcRenderer.send('toggle-sidebar'),
  // Sidebar-renderer-driven height hint: the floating menu's rendered
  // height is dynamic (one row per workspace + fixed bottom rows). The
  // sidebar WebContentsView used to span full window height, which made
  // its transparent area below the menu silently absorb clicks intended
  // for the workspace content underneath. The sidebar page posts its
  // rendered height here so the main process can shrink the view to
  // match and let clicks below the menu pass through to the content view.
  setSidebarHeight: (heightPx) => ipcRenderer.send('set-sidebar-height', heightPx),

  // Inbox modal (formerly the right-side requests panel)
  toggleInbox: () => ipcRenderer.send('toggle-inbox'),

  // Modal overlay close (used by the inbox shell and any one-off dialogs)
  closeModal: () => ipcRenderer.send('close-modal'),

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
});
