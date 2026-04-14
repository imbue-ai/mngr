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

  // Sidebar
  toggleSidebar: () => ipcRenderer.send('toggle-sidebar'),

  // Actions
  retry: () => ipcRenderer.send('retry'),
  openLogFile: () => ipcRenderer.send('open-log-file'),

  // Window controls
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),
});
