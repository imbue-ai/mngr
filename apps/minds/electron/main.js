const { app, BrowserWindow, Menu, ipcMain, shell } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');

todesktop.init();

let mainWindow = null;
let backendBaseUrl = null;

const isMac = process.platform === 'darwin';
const TITLEBAR_HEIGHT = 38;

// -- Title bar injection --
// Injected into every backend page via webContents APIs so the custom
// title bar persists across navigations.

const TITLEBAR_CSS = `
#minds-titlebar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  height: ${TITLEBAR_HEIGHT}px;
  background: #1e293b;
  display: flex;
  align-items: center;
  user-select: none;
  z-index: 2147483647;
  border-bottom: 1px solid #334155;
  padding: 0 4px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
#minds-titlebar button {
  background: none;
  border: none;
  color: #94a3b8;
  cursor: pointer;
  width: 32px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  font-size: 14px;
  line-height: 1;
}
#minds-titlebar button:hover { color: #e2e8f0; background: rgba(255,255,255,0.08); }
#minds-titlebar button:active { background: rgba(255,255,255,0.12); }
#minds-titlebar svg {
  width: 16px; height: 16px; fill: none; stroke: currentColor;
  stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
}
#minds-titlebar .minds-nav { display: flex; gap: 2px; }
#minds-titlebar .minds-title {
  flex: 1; color: #cbd5e1; font-size: 12px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  text-align: center; padding: 0 8px;
}
#minds-titlebar .minds-wc { display: flex; }
#minds-titlebar .minds-wc button { border-radius: 0; width: 36px; height: ${TITLEBAR_HEIGHT}px; }
#minds-titlebar .minds-wc button:hover { background: rgba(255,255,255,0.08); border-radius: 0; }
#minds-titlebar .minds-wc #minds-close:hover { background: #dc2626; color: white; }
body { padding-top: ${TITLEBAR_HEIGHT}px !important; }
`;

const TITLEBAR_CSS_MAC = `
#minds-titlebar { padding-left: 72px; }
#minds-titlebar .minds-wc { display: none; }
`;

const TITLEBAR_HTML = `
<div class="minds-nav">
  <button id="minds-home" title="Home">
    <svg viewBox="0 0 24 24"><path d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0h4"/></svg>
  </button>
  <button id="minds-back" title="Back">
    <svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
  </button>
  <button id="minds-forward" title="Forward">
    <svg viewBox="0 0 24 24"><polyline points="9 6 15 12 9 18"/></svg>
  </button>
</div>
<span class="minds-title" id="minds-title">Minds</span>
<button id="minds-external" title="Open in browser">
  <svg viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
</button>
<div class="minds-wc">
  <button id="minds-min" title="Minimize">
    <svg viewBox="0 0 12 12" style="width:12px;height:12px"><line x1="2" y1="6" x2="10" y2="6"/></svg>
  </button>
  <button id="minds-max" title="Maximize">
    <svg viewBox="0 0 12 12" style="width:12px;height:12px"><rect x="2" y="2" width="8" height="8" rx="0.5"/></svg>
  </button>
  <button id="minds-close" title="Close">
    <svg viewBox="0 0 12 12" style="width:12px;height:12px"><line x1="2" y1="2" x2="10" y2="10"/><line x1="10" y1="2" x2="2" y2="10"/></svg>
  </button>
</div>
`;

const TITLEBAR_JS = `(function() {
  if (document.getElementById('minds-titlebar')) return;
  var bar = document.createElement('div');
  bar.id = 'minds-titlebar';
  bar.innerHTML = ${JSON.stringify(TITLEBAR_HTML)};
  document.body.insertBefore(bar, document.body.firstChild);

  var titleEl = document.getElementById('minds-title');
  titleEl.textContent = document.title || 'Minds';

  var head = document.querySelector('head');
  if (head) {
    new MutationObserver(function() {
      titleEl.textContent = document.title || 'Minds';
    }).observe(head, { childList: true, subtree: true, characterData: true });
  }

  document.getElementById('minds-home').onclick = function() { if (window.minds) window.minds.goHome(); };
  document.getElementById('minds-back').onclick = function() { history.back(); };
  document.getElementById('minds-forward').onclick = function() { history.forward(); };
  document.getElementById('minds-external').onclick = function() {
    if (window.minds) window.minds.openExternal(location.href);
  };
  document.getElementById('minds-min').onclick = function() { if (window.minds) window.minds.minimize(); };
  document.getElementById('minds-max').onclick = function() { if (window.minds) window.minds.maximize(); };
  document.getElementById('minds-close').onclick = function() { if (window.minds) window.minds.close(); };
})();`;

// -- Single instance lock --
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(onReady);
}

async function onReady() {
  if (!isMac || process.env.MINDS_HIDE_MENU === '1') {
    Menu.setApplicationMenu(null);
  }

  createWindow();
  registerShortcuts();
  await runStartupSequence();
}

function createWindow() {
  const windowOptions = {
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: 'Minds',
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  };

  mainWindow = new BrowserWindow(windowOptions);

  mainWindow._maximizedByUs = false;
  mainWindow._boundsBeforeMaximize = null;
  mainWindow.on('maximize', () => { mainWindow._maximizedByUs = true; });
  mainWindow.on('unmaximize', () => { mainWindow._maximizedByUs = false; });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Inject the custom title bar into every backend page.
  // Skip file:// pages (loading/error screens).
  mainWindow.webContents.on('dom-ready', () => {
    const url = mainWindow.webContents.getURL();
    if (url.startsWith('file://')) return;

    const css = TITLEBAR_CSS + (isMac ? TITLEBAR_CSS_MAC : '');
    mainWindow.webContents.insertCSS(css);
    mainWindow.webContents.executeJavaScript(TITLEBAR_JS).catch((err) => {
      console.error('Failed to inject title bar JS:', err);
    });
  });
}

function registerShortcuts() {
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const devTools =
      (isMac && input.meta && input.alt && input.key.toLowerCase() === 'i') ||
      (!isMac && input.control && input.shift && input.key.toLowerCase() === 'c');
    if (devTools) {
      event.preventDefault();
      mainWindow.webContents.toggleDevTools();
    }
  });
}

async function runStartupSequence() {
  await mainWindow.loadFile(path.join(__dirname, 'shell.html'));

  try {
    await runEnvSetup((status) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('status-update', status);
      }
    });
  } catch (err) {
    showError(
      'Setup failed -- you may not be connected to the internet',
      err.message,
    );
    return;
  }

  await startBackendWithRetry();
}

async function startBackendWithRetry() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('status-update', 'Starting Minds...');
  }

  try {
    const { loginUrl, port } = await startBackend((status) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('status-update', status);
      }
    });

    backendBaseUrl = `http://127.0.0.1:${port}`;

    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadURL(loginUrl);
    }

    const proc = getBackendProcess();
    if (proc) {
      proc.on('exit', (code) => {
        if (mainWindow && !mainWindow.isDestroyed() && code !== 0 && code !== null) {
          const logContent = readLastLogLines(50);
          showError(
            'Minds stopped unexpectedly',
            logContent || `Process exited with code ${code}`,
          );
        }
      });
    }
  } catch (err) {
    showError('Failed to start Minds', err.message);
  }
}

function showError(message, details) {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  const url = mainWindow.webContents.getURL();
  if (!url.startsWith('file://')) {
    mainWindow.loadFile(path.join(__dirname, 'shell.html'));
    mainWindow.webContents.once('did-finish-load', () => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('error-details', { message, details });
      }
    });
  } else {
    mainWindow.webContents.send('error-details', { message, details });
  }
}

function readLastLogLines(lineCount) {
  try {
    const logPath = path.join(paths.getLogDir(), 'minds.log');
    if (!fs.existsSync(logPath)) return '';
    const content = fs.readFileSync(logPath, 'utf-8');
    const lines = content.split('\n');
    return lines.slice(-lineCount).join('\n');
  } catch {
    return '';
  }
}

// -- IPC handlers --

ipcMain.on('go-home', () => {
  if (mainWindow && !mainWindow.isDestroyed() && backendBaseUrl) {
    mainWindow.loadURL(backendBaseUrl + '/');
  }
});

ipcMain.on('open-external', (_event, url) => {
  if (typeof url === 'string') {
    try {
      const parsed = new URL(url);
      if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
        shell.openExternal(url);
      }
    } catch {
      // Invalid URL
    }
  }
});

ipcMain.on('retry', async () => {
  await shutdown();
  if (mainWindow && !mainWindow.isDestroyed()) {
    await mainWindow.loadFile(path.join(__dirname, 'shell.html'));
    startBackendWithRetry();
  }
});

ipcMain.on('open-log-file', () => {
  const logPath = path.join(paths.getLogDir(), 'minds.log');
  shell.openPath(logPath);
});

ipcMain.on('window-minimize', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.minimize();
  }
});

ipcMain.on('window-maximize', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMaximized() || mainWindow._maximizedByUs) {
      mainWindow.unmaximize();
      if (mainWindow._boundsBeforeMaximize) {
        mainWindow.setBounds(mainWindow._boundsBeforeMaximize);
        mainWindow._boundsBeforeMaximize = null;
      }
      mainWindow._maximizedByUs = false;
    } else {
      mainWindow._boundsBeforeMaximize = mainWindow.getBounds();
      mainWindow.maximize();
    }
  }
});

ipcMain.on('window-close', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.close();
  }
});

// -- App lifecycle --

let isShuttingDown = false;

app.on('window-all-closed', async () => {
  if (!isShuttingDown) {
    isShuttingDown = true;
    await shutdown();
    app.quit();
  }
});

app.on('before-quit', async (event) => {
  if (getBackendProcess() && !isShuttingDown) {
    isShuttingDown = true;
    event.preventDefault();
    await shutdown();
    app.quit();
  }
});
