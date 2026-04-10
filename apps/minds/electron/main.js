const { app, BaseWindow, WebContentsView, Menu, ipcMain, shell } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');

todesktop.init();

let mainWindow = null;
let titleBarView = null;
let contentView = null;
let backendBaseUrl = null;

const isMac = process.platform === 'darwin';
const TITLEBAR_HEIGHT = 38;

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
  // On Linux/Windows, always hide the menu (frameless windows have no menu bar).
  // On macOS, hide it only when MINDS_HIDE_MENU is set (the native menu bar is
  // visible by default with titleBarStyle: 'hiddenInset').
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
    frame: false,
    autoHideMenuBar: true,
  };

  if (isMac) {
    delete windowOptions.frame;
    windowOptions.titleBarStyle = 'hiddenInset';
  } else {
    windowOptions.titleBarStyle = 'hidden';
  }

  mainWindow = new BaseWindow(windowOptions);

  // Track maximize state explicitly for WMs that don't report it correctly
  mainWindow._maximizedByUs = false;
  mainWindow._boundsBeforeMaximize = null;
  mainWindow.on('maximize', () => { mainWindow._maximizedByUs = true; });
  mainWindow.on('unmaximize', () => { mainWindow._maximizedByUs = false; });

  const preloadPath = path.join(__dirname, 'preload.js');

  // Title bar view (persistent, loads titlebar.html)
  titleBarView = new WebContentsView({
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  // Content view (loads shell.html initially, then navigates to backend).
  // Added FIRST so it has lower z-order (title bar sits on top).
  contentView = new WebContentsView({
    webPreferences: {
      preload: preloadPath,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.contentView.addChildView(contentView);

  // Title bar view (added SECOND so it has higher z-order and receives
  // click events in its 38px area without blocking the content view below).
  mainWindow.contentView.addChildView(titleBarView);
  titleBarView.webContents.loadFile(path.join(__dirname, 'titlebar.html'));

  mainWindow.on('resize', updateViewBounds);

  // Forward page title changes from content view to title bar view
  contentView.webContents.on('page-title-updated', (_event, title) => {
    if (titleBarView && !titleBarView.webContents.isDestroyed()) {
      titleBarView.webContents.send('title-update', title);
    }
  });

  // Show the window once the content view finishes its initial load,
  // then set view bounds (getContentBounds is reliable after show).
  contentView.webContents.once('did-finish-load', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.show();
      updateViewBounds();
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
    titleBarView = null;
    contentView = null;
  });
}

function updateViewBounds() {
  if (!mainWindow || !titleBarView || !contentView) return;
  const { width, height } = mainWindow.getContentBounds();
  titleBarView.setBounds({ x: 0, y: 0, width, height: TITLEBAR_HEIGHT });
  contentView.setBounds({ x: 0, y: TITLEBAR_HEIGHT, width, height: height - TITLEBAR_HEIGHT });
}

function registerShortcuts() {
  // Window-local shortcut: Open DevTools with Ctrl+Shift+C (Win/Linux) or Cmd+Option+I (macOS).
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
  // Load the loading screen in the content view
  await contentView.webContents.loadFile(path.join(__dirname, 'shell.html'));

  // Step 1: Run env setup (uv sync)
  try {
    await runEnvSetup((status) => {
      if (contentView && !contentView.webContents.isDestroyed()) {
        contentView.webContents.send('status-update', status);
      }
    });
  } catch (err) {
    showError(
      'Setup failed -- you may not be connected to the internet',
      err.message,
    );
    return;
  }

  // Step 2: Start backend
  await startBackendWithRetry();
}

async function startBackendWithRetry() {
  if (contentView && !contentView.webContents.isDestroyed()) {
    contentView.webContents.send('status-update', 'Starting Minds...');
  }

  try {
    const { loginUrl, port } = await startBackend((status) => {
      if (contentView && !contentView.webContents.isDestroyed()) {
        contentView.webContents.send('status-update', status);
      }
    });

    backendBaseUrl = `http://127.0.0.1:${port}`;

    // Navigate the content view to the login URL
    if (contentView && !contentView.webContents.isDestroyed()) {
      contentView.webContents.loadURL(loginUrl);
    }

    // Monitor for unexpected exits
    const proc = getBackendProcess();
    if (proc) {
      proc.on('exit', (code) => {
        if (contentView && !contentView.webContents.isDestroyed() && code !== 0 && code !== null) {
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
  if (!contentView || contentView.webContents.isDestroyed()) return;

  const url = contentView.webContents.getURL();
  if (!url.startsWith('file://')) {
    contentView.webContents.loadFile(path.join(__dirname, 'shell.html'));
    contentView.webContents.once('did-finish-load', () => {
      if (contentView && !contentView.webContents.isDestroyed()) {
        contentView.webContents.send('error-details', { message, details });
      }
    });
  } else {
    contentView.webContents.send('error-details', { message, details });
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

// Navigation (from title bar)
ipcMain.on('go-back', () => {
  if (contentView && !contentView.webContents.isDestroyed()) {
    contentView.webContents.goBack();
  }
});

ipcMain.on('go-forward', () => {
  if (contentView && !contentView.webContents.isDestroyed()) {
    contentView.webContents.goForward();
  }
});

ipcMain.on('go-home', () => {
  if (contentView && !contentView.webContents.isDestroyed() && backendBaseUrl) {
    contentView.webContents.loadURL(backendBaseUrl + '/');
  }
});

ipcMain.on('open-external', () => {
  if (contentView && !contentView.webContents.isDestroyed()) {
    const url = contentView.webContents.getURL();
    if (url && !url.startsWith('file://')) {
      try {
        const parsed = new URL(url);
        if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
          shell.openExternal(url);
        }
      } catch {
        // Invalid URL, ignore
      }
    }
  }
});

ipcMain.on('retry', async () => {
  await shutdown();
  if (contentView && !contentView.webContents.isDestroyed()) {
    await contentView.webContents.loadFile(path.join(__dirname, 'shell.html'));
    startBackendWithRetry();
  }
});

ipcMain.on('open-log-file', () => {
  const logPath = path.join(paths.getLogDir(), 'minds.log');
  shell.openPath(logPath);
});

// Window control handlers
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
