// Narrow preload attached to the content view only. The content view loads
// pages that can be shaped by agent-controlled code (the workspace_server
// frontend, served from inside the agent's sandbox), so we deliberately do
// not expose the full `window.minds` surface from preload.js here -- window
// controls, navigation, retry, etc. stay reserved for the chrome-origin
// views (chromeView / sidebarView / requestsPanelView).
//
// The only capability exposed here is registering a cmd+W close-tab handler.
// When the user presses cmd+W, main.js sends 'close-active-tab-request' with
// a correlation id; this preload forwards that to the handler the renderer
// registered (typically DockviewWorkspace closing its active panel) and
// sends 'close-active-tab-response' back with the handler's boolean result.
// Main falls back to closing the window when the response is false.

const { contextBridge, ipcRenderer } = require('electron');

let closeActiveTabHandler = null;

ipcRenderer.on('close-active-tab-request', async (_event, requestId) => {
  let closed = false;
  try {
    if (typeof closeActiveTabHandler === 'function') {
      closed = !!(await closeActiveTabHandler());
    }
  } catch (err) {
    console.error('close-active-tab handler threw:', err);
    closed = false;
  }
  ipcRenderer.send('close-active-tab-response', requestId, closed);
});

contextBridge.exposeInMainWorld('minds', {
  setCloseActiveTabHandler: (handler) => {
    closeActiveTabHandler = typeof handler === 'function' ? handler : null;
  },
});
