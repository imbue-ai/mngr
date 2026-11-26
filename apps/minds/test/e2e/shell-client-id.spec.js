// End-to-end test of the shell client id, driving a real Electron process
// (Playwright's `_electron`) on shell-client-id-app.js, twice on one profile.
// What it pins beyond the unit tests is Electron's and Chromium's side of the
// contract: that the default session's localStorage entry for a shell framed
// under the Imbue Studio page can be read back through a copy of its
// directory; that the preload runs in a window's out-of-process frames, where
// its synchronous ask reaches main with the asking frame and its parent, and
// that the id it writes is the one the shell reads as it boots; and that
// neither a workspace app's frame nor a frame the shell nests gets the bridge.

const path = require('path');
const os = require('os');
const fs = require('fs');
const { _electron } = require('playwright');
const { test, expect } = require('@playwright/test');
const { waitFor } = require('./wait-for');

const REPO_ROOT = path.resolve(__dirname, '../../../..');
const ELECTRON_BIN = path.join(REPO_ROOT, 'apps/minds/node_modules/.bin/electron');
const APP_JS = path.join(__dirname, 'shell-client-id-app.js');
// Each phase launches Electron and loads every window's frames before it reports.
const PHASE_TIMEOUT_MS = 30_000;

const harness = (electronApp) => electronApp.evaluate(() => globalThis.__shellClientIdHarness);

function launch(userDataDir, phase) {
  return _electron.launch({
    executablePath: ELECTRON_BIN,
    args: [APP_JS, `--phase=${phase}`, `--user-data-dir=${userDataDir}`],
    timeout: 60_000,
  });
}

// What each window's frames saw, through Electron's own frame handles:
// Playwright's frame list can miss out-of-process frames.
function readWindows(electronApp) {
  return electronApp.evaluate(async ({ BrowserWindow }) => {
    const { shellUrl, appUrl, nestedUrl } = globalThis.__shellClientIdHarness;
    const read = (frame, code) => (frame ? frame.executeJavaScript(code) : 'no frame');
    return Promise.all(BrowserWindow.getAllWindows().map(async (win) => {
      const frames = win.webContents.mainFrame.framesInSubtree;
      const shell = frames.find((frame) => frame.url === shellUrl);
      return {
        partition: win.webContents.session.getStoragePath().split('/').pop(),
        studioPageHasBridge: await read(win.webContents.mainFrame, 'window.__hasBridge'),
        shellBootId: await read(shell, 'window.__bootId'),
        shellHasBridge: await read(shell, 'window.__hasBridge'),
        appHasBridge: await read(frames.find((frame) => frame.url === appUrl), 'window.__hasBridge'),
        nestedHasBridge: await read(frames.find((frame) => frame.url === nestedUrl), 'window.__hasBridge'),
      };
    }));
  });
}

test('every partition\'s shell boots as the client the default session knew it as', async () => {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-shell-client-id-'));
  try {
    const seeding = await launch(userDataDir, 'seed');
    try {
      await waitFor(() => harness(seeding), (state) => state.isSeeded || state.uncaughtErrors.length > 0, 'the seeded profile', PHASE_TIMEOUT_MS);
      expect((await harness(seeding)).uncaughtErrors).toEqual([]);
    } finally {
      await seeding.close().catch(() => {});
    }

    const electronApp = await launch(userDataDir, 'run');
    try {
      const state = await waitFor(() => harness(electronApp), (current) => current.isReady || current.uncaughtErrors.length > 0, 'both windows', PHASE_TIMEOUT_MS);
      expect(state.uncaughtErrors).toEqual([]);
      const shellHostname = new URL(state.shellUrl).hostname;
      expect(state.imported).toEqual({ [shellHostname]: '6a8bc152-a4af-4c08-a240-7d80ba93757e' });

      const windows = await waitFor(
        () => readWindows(electronApp),
        (seen) => seen.length === 2 && seen.every((win) => typeof win.shellBootId === 'string' && typeof win.nestedHasBridge === 'boolean'),
        'every frame of both windows',
        PHASE_TIMEOUT_MS,
      );
      expect(windows.map((win) => win.partition).sort()).toEqual(['account-0b8d1c5e', 'account-local']);
      for (const win of windows) {
        expect(win).toMatchObject({
          studioPageHasBridge: true,
          shellBootId: '6a8bc152-a4af-4c08-a240-7d80ba93757e',
          shellHasBridge: false,
          appHasBridge: false,
          nestedHasBridge: false,
        });
      }
      const stored = JSON.parse(fs.readFileSync(path.join(userDataDir, 'shell-client-ids.json'), 'utf8'));
      expect(stored).toEqual({ version: 1, is_legacy_import_done: true, ids: { [shellHostname]: '6a8bc152-a4af-4c08-a240-7d80ba93757e' } });
    } finally {
      await electronApp.close().catch(() => {});
    }
  } finally {
    fs.rmSync(userDataDir, { recursive: true, force: true });
  }
});
