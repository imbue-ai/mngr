const { app, BrowserWindow, ipcMain, shell } = require('electron');
const todesktop = require('@todesktop/runtime');
const path = require('path');
const fs = require('fs');
const paths = require('./paths');
const { runEnvSetup } = require('./env-setup');
const { startBackend, shutdown, getBackendProcess } = require('./backend');

todesktop.init();

let mainWindow = null;

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
  createWindow();
  await runStartupSequence();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: 'Minds',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

async function runStartupSequence() {
  // Step 1: Show loading screen
  mainWindow.loadFile(path.join(__dirname, 'loading.html'));

  // Step 2: Run env setup (uv sync)
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

  // Step 3: Start backend
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

    // Navigate to the login URL
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.loadURL(loginUrl);
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

  mainWindow.loadFile(path.join(__dirname, 'error.html'));

  // Wait for the page to load before sending error details
  mainWindow.webContents.once('did-finish-load', () => {
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('error-details', { message, details });
    }
  });
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
    mainWindow.loadFile(path.join(__dirname, 'loading.html'));
    // Brief delay to let the loading page render
    setTimeout(() => startBackendWithRetry(), 100);
  }
});

ipcMain.on('open-log-file', () => {
  const logPath = path.join(paths.getLogDir(), 'minds.log');
  shell.openPath(logPath);
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
