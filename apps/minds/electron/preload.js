const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('minds', {
  onStatusUpdate: (callback) => {
    ipcRenderer.on('status-update', (_event, message) => callback(message));
  },
  onErrorDetails: (callback) => {
    ipcRenderer.on('error-details', (_event, details) => callback(details));
  },
  retry: () => ipcRenderer.send('retry'),
  openLogFile: () => ipcRenderer.send('open-log-file'),
});
