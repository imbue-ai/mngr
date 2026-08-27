const { contextBridge, ipcRenderer } = require('electron');

// The slim native bridge. The window's page is the Mithril SPA served by the
// app server; it owns navigation, modals, and all content handling in-page
// (frontend/src/electron-bridge.ts is the typed facade over this object).
// Only genuinely native affordances remain here: window controls, native
// dialogs, the renderer-to-main shell-event relay, the release-channel and
// update-status calls (there is no binary to update in a browser), and the
// startup/error/quitting shell.html channels.
contextBridge.exposeInMainWorld('mindsNative', {
  platform: process.platform,

  // Startup / error / quitting screens (shell.html).
  onStatusUpdate: (callback) => {
    ipcRenderer.on('status-update', (_event, message) => callback(message));
  },
  onErrorDetails: (callback) => {
    ipcRenderer.on('error-details', (_event, details) => callback(details));
  },
  retry: () => ipcRenderer.send('retry'),
  openLogFile: () => ipcRenderer.send('open-log-file'),
  // One-shot bug report from the full-app error takeover, via the
  // main-process Sentry (the backend and its /help flow may be down).
  reportError: () => ipcRenderer.invoke('report-error'),
  // Reload after the window's renderer showed the crash strip.
  reloadChrome: () => ipcRenderer.send('reload-chrome'),

  // Window controls (non-macOS custom titlebar buttons).
  minimize: () => ipcRenderer.send('window-minimize'),
  maximize: () => ipcRenderer.send('window-maximize'),
  close: () => ipcRenderer.send('window-close'),

  // Native file/directory picker (the file-sharing permission dialog).
  showFilePicker: (options) => ipcRenderer.invoke('show-file-picker', options),

  // Open the OS's own notification-settings pane (no app can force a
  // re-prompt once declined -- the reader has to flip it back on there).
  openNotificationSettings: () => ipcRenderer.invoke('open-notification-settings'),

  // Bring the app back in front after an external-browser OAuth hop.
  bringAppToFront: () => ipcRenderer.send('bring-app-to-front'),

  // Multi-window (desktop-only concept).
  openWorkspaceInNewWindow: (agentId) => ipcRenderer.send('open-workspace-in-new-window', agentId),

  // Release channels. Desktop-only: the web UI has no binary to update, so the
  // Settings section that uses these renders only when mindsNative is present.
  getUpdateState: () => ipcRenderer.invoke('get-update-state'),
  peekUpdateChannels: () => ipcRenderer.invoke('peek-update-channels'),
  setUpdateChannel: (channel) => ipcRenderer.invoke('set-update-channel', channel),
  checkForUpdates: () => ipcRenderer.invoke('check-for-updates'),
  installUpdate: () => ipcRenderer.invoke('install-update'),
  onUpdateStatus: (callback) => {
    ipcRenderer.on('update-status', (_event, status) => callback(status));
  },

  // The renderer owns the /ui/ws channel; the few events main still acts on
  // (workspaces summaries for OS titles + destroyed-window detach,
  // system-interface health, workspace_stopped, open_help routing, the
  // notification feed's unresolved count for the dock/taskbar badge) are
  // relayed up through this one channel.
  sendShellEvent: (event) => ipcRenderer.send('shell-event', event),

  // Main-process asks (notifications, deeplinks, main-driven routing).
  onNavigate: (callback) => {
    ipcRenderer.on('shell-navigate', (_event, url) => callback(url));
  },
  onOpenOverlay: (callback) => {
    ipcRenderer.on('open-overlay', (_event, cmd) => callback(cmd));
  },
  // Cmd/Ctrl+W while the workspace iframe has focus: seen by main's
  // before-input-event, relayed into the workspace via the embed contract.
  onCloseActiveTab: (callback) => {
    ipcRenderer.on('close-active-tab', () => callback());
  },
  // Escape backstop: keydowns inside the workspace iframe don't reach the
  // chrome page's own listeners.
  onEscapePressed: (callback) => {
    ipcRenderer.on('escape-pressed', () => callback());
  },
});
