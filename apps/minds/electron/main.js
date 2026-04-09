const { app, BrowserWindow, Menu, ipcMain, shell, globalShortcut } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');

todesktop.init();

let mainWindow = null;

const isMac = process.platform === 'darwin';

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
  // Hide the application menu if MINDS_HIDE_MENU is set
  if (process.env.MINDS_HIDE_MENU === '1') {
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
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  };

  // On macOS, use hiddenInset to preserve native traffic light buttons
  if (isMac) {
    windowOptions.frame = undefined;
    windowOptions.titleBarStyle = 'hiddenInset';
  }

  mainWindow = new BrowserWindow(windowOptions);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function registerShortcuts() {
  // Open DevTools: Ctrl+Shift+C on Windows/Linux, Cmd+Option+I on macOS
  const devToolsAccelerator = isMac ? 'Cmd+Option+I' : 'Ctrl+Shift+C';
  globalShortcut.register(devToolsAccelerator, () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.toggleDevTools();
    }
  });
}

async function runStartupSequence() {
  // Load the shell (custom title bar + content area)
  mainWindow.loadFile(path.join(__dirname, 'shell.html'));

  // Step 1: Run env setup (uv sync)
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

  // Step 2: Start backend
  await startBackendWithRetry();
}

async function startBackendWithRetry() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('status-update', 'Starting Minds...');
  }

  try {
    const { loginUrl } = await startBackend((status) => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        mainWindow.webContents.send('status-update', status);
      }
    });

    // Tell the shell to navigate the content iframe to the login URL
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('navigate', loginUrl);
    }

    // Monitor for unexpected exits
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
  mainWindow.webContents.send('error-details', { message, details });
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

ipcMain.on('retry', async () => {
  // Shut down any existing backend before retrying
  await shutdown();
  if (mainWindow && !mainWindow.isDestroyed()) {
    // Reload the shell to reset state, then start backend again
    mainWindow.loadFile(path.join(__dirname, 'shell.html'));
    mainWindow.webContents.once('did-finish-load', () => {
      startBackendWithRetry();
    });
  }
});

ipcMain.on('open-log-file', () => {
  const logPath = path.join(paths.getLogDir(), 'minds.log');
  shell.openPath(logPath);
});

ipcMain.on('open-external', (_event, url) => {
  if (url && typeof url === 'string') {
    shell.openExternal(url);
  }
});

// Window control handlers
ipcMain.on('window-minimize', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.minimize();
  }
});

ipcMain.on('window-maximize', () => {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMaximized()) {
      mainWindow.unmaximize();
    } else {
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

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});
