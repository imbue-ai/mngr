const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('minds', {
  // Platform info
  platform: process.platform,

  // Status and error callbacks (used by shell.html loading/error screen in content view)
  onStatusUpdate: (callback) => {
    ipcRenderer.on('status-update', (_event, message) => callback(message));
  },
  onErrorDetails: (callback) => {
    ipcRenderer.on('error-details', (_event, details) => callback(details));
  },

  // Title bar callbacks (used by titlebar.html)
  onTitleUpdate: (callback) => {
    ipcRenderer.on('title-update', (_event, title) => callback(title));
  },

  // Content navigation (title bar -> main process -> content view)
  goBack: () => ipcRenderer.send('go-back'),
  goForward: () => ipcRenderer.send('go-forward'),
  goHome: () => ipcRenderer.send('go-home'),

  // Actions
  retry: () => ipcRenderer.send('retry'),
  openLogFile: () => ipcRenderer.send('open-log-file'),
  openExternal: () => ipcRenderer.send('open-external'),

  // Window controls
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),
});
