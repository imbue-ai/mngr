const { BaseWindow, WebContentsView, Menu, Notification, ipcMain, shell } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');
const limaInstall = require('./lima-install');

todesktop.init({
  updateReadyAction: {
    showInstallAndRestartPrompt: 'always',
  },
});

let mainWindow = null;
let chromeView = null;
let contentView = null;
let sidebarView = null;
let requestsPanelView = null;
let backendBaseUrl = null;

const isMac = process.platform === 'darwin';
const TITLEBAR_HEIGHT = 38;
const SIDEBAR_WIDTH = 260;
const REQUESTS_PANEL_WIDTH = 320;

// -- Single instance lock --
const gotLock = require('electron').app.requestSingleInstanceLock();
if (!gotLock) {
  require('electron').app.quit();
} else {
  require('electron').app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  require('electron').app.whenReady().then(onReady);
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
  };

  if (isMac) {
    windowOptions.titleBarStyle = 'hiddenInset';
    windowOptions.trafficLightPosition = { x: 12, y: (TITLEBAR_HEIGHT - 16) / 2 };
  } else {
    windowOptions.frame = false;
  }

  mainWindow = new BaseWindow(windowOptions);

  // Create chrome view (title bar) -- loads /_chrome from backend
  chromeView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // Create content view -- loads page content (landing page, workspaces, etc.)
  contentView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.contentView.addChildView(chromeView);
  mainWindow.contentView.addChildView(contentView);

  updateViewBounds();

  mainWindow._maximizedByUs = false;
  mainWindow._boundsBeforeMaximize = null;
  mainWindow.on('maximize', () => { mainWindow._maximizedByUs = true; });
  mainWindow.on('unmaximize', () => { mainWindow._maximizedByUs = false; });

  mainWindow.once('ready-to-show', () => {
    console.log('[window] ready-to-show fired');
    mainWindow.show();
  });

  // Auto-open DevTools when MINDS_OPEN_DEVTOOLS=1 is set. The built-in
  // ⌘+⌥+I shortcut hits an Electron menu handler that assumes a
  // BrowserWindow (we use BaseWindow + WebContentsViews) and crashes;
  // this env var is the escape hatch for dev-time inspection.
  if (process.env.MINDS_OPEN_DEVTOOLS === '1') {
    contentView.webContents.once('did-finish-load', () => {
      contentView.webContents.openDevTools({ mode: 'detach' });
    });
  }

  // BaseWindow may not fire ready-to-show since it has no built-in web contents.
  // Show the window immediately after a short delay as a fallback.
  setTimeout(() => {
    if (mainWindow && !mainWindow.isDestroyed() && !mainWindow.isVisible()) {
      console.log('[window] Showing window via fallback timeout');
      mainWindow.show();
    }
  }, 500);

  mainWindow.on('closed', () => {
    mainWindow = null;
    chromeView = null;
    contentView = null;
    sidebarView = null;
    requestsPanelView = null;
  });

  mainWindow.on('resize', updateViewBounds);

  // Forward content view navigation events to chrome view
  contentView.webContents.on('page-title-updated', (_event, title) => {
    if (chromeView && !chromeView.webContents.isDestroyed()) {
      chromeView.webContents.send('content-title-changed', title);
    }
  });

  contentView.webContents.on('did-navigate', (_event, url) => {
    if (chromeView && !chromeView.webContents.isDestroyed()) {
      chromeView.webContents.send('content-url-changed', url);
    }
  });

  contentView.webContents.on('did-navigate-in-page', (_event, url) => {
    if (chromeView && !chromeView.webContents.isDestroyed()) {
      chromeView.webContents.send('content-url-changed', url);
    }
  });
}

function updateViewBounds() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  const { width, height } = mainWindow.getContentBounds();

  if (chromeView) {
    chromeView.setBounds({ x: 0, y: 0, width, height: TITLEBAR_HEIGHT });
  }
  if (contentView) {
    const rightOffset = requestsPanelView ? REQUESTS_PANEL_WIDTH : 0;
    contentView.setBounds({ x: 0, y: TITLEBAR_HEIGHT, width: width - rightOffset, height: height - TITLEBAR_HEIGHT });
  }
  if (sidebarView) {
    sidebarView.setBounds({ x: 0, y: TITLEBAR_HEIGHT, width: SIDEBAR_WIDTH, height: height - TITLEBAR_HEIGHT });
  }
  if (requestsPanelView) {
    requestsPanelView.setBounds({
      x: width - REQUESTS_PANEL_WIDTH,
      y: TITLEBAR_HEIGHT,
      width: REQUESTS_PANEL_WIDTH,
      height: height - TITLEBAR_HEIGHT,
    });
  }
}

function toggleSidebar() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  if (sidebarView) {
    // Remove sidebar
    mainWindow.contentView.removeChildView(sidebarView);
    sidebarView.webContents.close();
    sidebarView = null;
  } else {
    // Create and show sidebar
    sidebarView = new WebContentsView({
      webPreferences: {
        preload: path.join(__dirname, 'preload.js'),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    mainWindow.contentView.addChildView(sidebarView);
    updateViewBounds();

    if (backendBaseUrl) {
      sidebarView.webContents.loadURL(backendBaseUrl + '/_chrome/sidebar');
    }
  }
}

function toggleRequestsPanel() {
  if (!mainWindow || mainWindow.isDestroyed()) return;

  if (requestsPanelView) {
    // Remove requests panel
    mainWindow.contentView.removeChildView(requestsPanelView);
    requestsPanelView.webContents.close();
    requestsPanelView = null;
    updateViewBounds();
  } else {
    openRequestsPanel();
  }
}

function openRequestsPanel() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  if (requestsPanelView) return; // Already open

  requestsPanelView = new WebContentsView({
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.contentView.addChildView(requestsPanelView);
  updateViewBounds();

  if (backendBaseUrl) {
    requestsPanelView.webContents.loadURL(backendBaseUrl + '/_chrome/requests-panel');
  }
}

function registerShortcuts() {
  chromeView.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const devTools =
      (isMac && input.meta && input.alt && input.key.toLowerCase() === 'i') ||
      (!isMac && input.control && input.shift && input.key.toLowerCase() === 'c');
    if (devTools) {
      event.preventDefault();
      contentView.webContents.toggleDevTools();
    }
  });

  contentView.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return;
    const devTools =
      (isMac && input.meta && input.alt && input.key.toLowerCase() === 'i') ||
      (!isMac && input.control && input.shift && input.key.toLowerCase() === 'c');
    if (devTools) {
      event.preventDefault();
      contentView.webContents.toggleDevTools();
    }
  });
}

async function runStartupSequence() {
  console.log('[startup] Loading shell.html in chrome view...');
  // During startup, expand chrome view to full window to show loading screen
  if (mainWindow && !mainWindow.isDestroyed()) {
    const { width, height } = mainWindow.getContentBounds();
    chromeView.setBounds({ x: 0, y: 0, width, height });
    // Hide content view during startup
    contentView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
  }
  await chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
  console.log('[startup] shell.html loaded');

  try {
    await runEnvSetup((status) => {
      if (chromeView && !chromeView.webContents.isDestroyed()) {
        chromeView.webContents.send('status-update', status);
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
  if (chromeView && !chromeView.webContents.isDestroyed()) {
    chromeView.webContents.send('status-update', 'Starting Minds...');
  }

  try {
    const { loginUrl, port } = await startBackend((status) => {
      if (chromeView && !chromeView.webContents.isDestroyed()) {
        chromeView.webContents.send('status-update', status);
      }
    }, (event) => {
      const agentName = event.agent_name || 'Agent';
      const title = event.title || `Notification from ${agentName}`;
      const notification = new Notification({
        title,
        body: event.message,
      });
      notification.on('click', () => {
        if (mainWindow && !mainWindow.isDestroyed()) {
          if (mainWindow.isMinimized()) mainWindow.restore();
          mainWindow.focus();
          if (event.url && contentView && !contentView.webContents.isDestroyed()) {
            const navUrl = event.url.startsWith('/') ? `http://127.0.0.1:${port}${event.url}` : event.url;
            contentView.webContents.loadURL(navUrl);
          }
        }
      });
      notification.show();
    }, (event) => {
      if (!mainWindow || mainWindow.isDestroyed()) return;
      if (event.event === 'auth_success') {
        // Reload chrome to update auth state
        if (chromeView && !chromeView.webContents.isDestroyed()) {
          chromeView.webContents.reload();
        }
      } else if (event.event === 'auth_required') {
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.show();
        mainWindow.focus();
        const authUrl = `http://127.0.0.1:${port}/auth/login?message=` +
          encodeURIComponent('You need to sign in to Imbue in order to share');
        if (contentView && !contentView.webContents.isDestroyed()) {
          contentView.webContents.loadURL(authUrl);
        }
      }
    });

    backendBaseUrl = `http://127.0.0.1:${port}`;

    console.log('[startup] Backend ready. Loading chrome from', backendBaseUrl + '/_chrome');
    console.log('[startup] Loading content from', loginUrl);

    // Restore normal layout: chrome at top, content below
    updateViewBounds();

    // Load chrome from backend and content from landing page
    if (chromeView && !chromeView.webContents.isDestroyed()) {
      chromeView.webContents.loadURL(backendBaseUrl + '/_chrome');
    }
    if (contentView && !contentView.webContents.isDestroyed()) {
      contentView.webContents.loadURL(loginUrl);
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

  // Remove sidebar and content views on error
  if (sidebarView) {
    mainWindow.contentView.removeChildView(sidebarView);
    sidebarView.webContents.close();
    sidebarView = null;
  }
  if (contentView) {
    mainWindow.contentView.removeChildView(contentView);
    contentView.webContents.close();
    contentView = null;
  }

  // Expand chrome view to fill the window for the error screen
  if (mainWindow && !mainWindow.isDestroyed()) {
    const { width, height } = mainWindow.getContentBounds();
    if (chromeView) {
      chromeView.setBounds({ x: 0, y: 0, width, height });
    }
  }

  if (chromeView && !chromeView.webContents.isDestroyed()) {
    const url = chromeView.webContents.getURL();
    if (!url.startsWith('file://')) {
      chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
      chromeView.webContents.once('did-finish-load', () => {
        if (chromeView && !chromeView.webContents.isDestroyed()) {
          chromeView.webContents.send('error-details', { message, details });
        }
      });
    } else {
      chromeView.webContents.send('error-details', { message, details });
    }
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
  if (contentView && !contentView.webContents.isDestroyed() && backendBaseUrl) {
    contentView.webContents.loadURL(backendBaseUrl + '/');
  }
});

ipcMain.on('navigate-content', (_event, url) => {
  if (contentView && !contentView.webContents.isDestroyed()) {
    // If the URL is relative, prepend the backend base URL
    if (url.startsWith('/') && backendBaseUrl) {
      url = backendBaseUrl + url;
    }
    contentView.webContents.loadURL(url);
  }
  // Close sidebar after navigation
  if (sidebarView && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.contentView.removeChildView(sidebarView);
    sidebarView.webContents.close();
    sidebarView = null;
  }
});

ipcMain.on('content-go-back', () => {
  if (contentView && !contentView.webContents.isDestroyed()) {
    contentView.webContents.goBack();
  }
});

ipcMain.on('content-go-forward', () => {
  if (contentView && !contentView.webContents.isDestroyed()) {
    contentView.webContents.goForward();
  }
});

ipcMain.on('toggle-sidebar', () => {
  toggleSidebar();
});

ipcMain.on('toggle-requests-panel', () => {
  toggleRequestsPanel();
});

ipcMain.on('open-requests-panel', () => {
  openRequestsPanel();
});

ipcMain.on('retry', async () => {
  await shutdown();

  // Recreate content view if it was removed during error
  if (!contentView && mainWindow && !mainWindow.isDestroyed()) {
    contentView = new WebContentsView({
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    // chromeView is never removed during the error path, so only add contentView
    mainWindow.contentView.addChildView(contentView);
    updateViewBounds();
  }

  if (chromeView && !chromeView.webContents.isDestroyed()) {
    // Expand chrome view to full window and hide content view for the loading screen
    if (mainWindow && !mainWindow.isDestroyed()) {
      const { width, height } = mainWindow.getContentBounds();
      chromeView.setBounds({ x: 0, y: 0, width, height });
      if (contentView) {
        contentView.setBounds({ x: 0, y: 0, width: 0, height: 0 });
      }
    }
    await chromeView.webContents.loadFile(path.join(__dirname, 'shell.html'));
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

// -- Lima lazy install --
// The renderer (create form) calls ensure-lima when the user picks LIMA
// mode. We emit lima-progress events during download so the form can
// show an inline progress pill. Resolves immediately if limactl is
// already cached or installed on the system PATH.
ipcMain.handle('ensure-lima', async (event) => {
  try {
    const result = await limaInstall.ensureLima((pct) => {
      if (!event.sender.isDestroyed()) {
        event.sender.send('lima-progress', pct);
      }
    });
    return { ok: true, source: result.source };
  } catch (err) {
    return { ok: false, error: err.message };
  }
});

ipcMain.handle('is-lima-available', async () => {
  return { available: limaInstall.isLimaAvailable() };
});

// -- App lifecycle --

let isShuttingDown = false;

require('electron').app.on('window-all-closed', async () => {
  console.log('[lifecycle] window-all-closed fired, isShuttingDown=' + isShuttingDown);
  if (!isShuttingDown) {
    isShuttingDown = true;
    console.log('[lifecycle] Starting shutdown from window-all-closed...');
    await shutdown();
    console.log('[lifecycle] Shutdown complete, calling app.quit()');
    require('electron').app.quit();
  }
});

require('electron').app.on('before-quit', async (event) => {
  console.log('[lifecycle] before-quit fired, isShuttingDown=' + isShuttingDown + ', hasBackend=' + !!getBackendProcess());
  if (getBackendProcess() && !isShuttingDown) {
    isShuttingDown = true;
    event.preventDefault();
    console.log('[lifecycle] Starting shutdown from before-quit...');
    await shutdown();
    console.log('[lifecycle] Shutdown complete, calling app.quit()');
    require('electron').app.quit();
  }
});
