// Shared Playwright fixtures for minds.app UI tests.
//
// `mindsApp` launches the installed /Applications/Minds.app binary under an
// isolated MINDS_ROOT_NAME so the user's live `~/.minds/` state is neither
// destroyed nor inherited. Yields { app, mainWindow, hostDir } and tears the
// Electron app down on test exit.

const path = require('path');
const fs = require('fs');
const os = require('os');
const { _electron: electron } = require('playwright');
const base = require('@playwright/test');

const DEFAULT_APP_PATH = '/Applications/Minds.app/Contents/MacOS/Minds';

const test = base.test.extend({
  mindsApp: async ({}, use, testInfo) => {
    const execPath = process.env.MINDS_APP_PATH || DEFAULT_APP_PATH;
    if (!fs.existsSync(execPath)) {
      throw new Error(
        `minds.app binary not found at ${execPath}. Install it to /Applications/ or ` +
          `set MINDS_APP_PATH to a downloaded build.`
      );
    }

    // Per-run isolated host dir + a sentinel MINDS_ROOT_NAME so paths.js
    // resolves ~/.minds-playwright-<runId>/ for cookies, venv, mngr state.
    // The sentinel must match `minds(-<env-name>)?` (paths.js regex), so a
    // hex run id keeps it valid.
    const runId = Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    const rootName = `minds-pw-${runId}`;
    const hostDir = path.join(os.homedir(), `.${rootName}`);

    const app = await electron.launch({
      executablePath: execPath,
      env: {
        ...process.env,
        MINDS_ROOT_NAME: rootName,
        MINDS_TEST_MODE: '1',
      },
      timeout: 5 * 60 * 1000,
    });

    // The chat-chrome window is the first/only top-level BrowserWindow.
    const mainWindow = await app.firstWindow({ timeout: 5 * 60 * 1000 });

    await use({ app, mainWindow, hostDir, rootName });

    // Capture Electron stdout/stderr to the test output dir on failure.
    if (testInfo.status !== 'passed') {
      const evtLog = path.join(hostDir, 'logs', 'minds-events.jsonl');
      const mainLog = path.join(hostDir, 'logs', 'minds.log');
      for (const src of [evtLog, mainLog]) {
        if (fs.existsSync(src)) {
          const dst = path.join(testInfo.outputDir, path.basename(src));
          fs.copyFileSync(src, dst);
        }
      }
    }

    await app.close();
  },
});

module.exports = { test, expect: base.expect };
