// End-to-end tests of the loading document's intro handshake on a real Electron
// window running intro-app.js: the shipped shell.html#intro and preload, with
// the harness standing in for main.js by sending `startup-ready` and recording
// when `intro-finished` came back.
//
// Main lands the first route only once the page reports the intro over, so the
// page must report it whether or not anyone can watch the film. A hidden window
// (minimized, hidden, or on macOS fully covered) gets no animation frames, and
// the mark's arrival runs on them.
//
// No installed app or backend needed. From apps/minds:
//   pnpm exec playwright test --config=test/e2e/playwright.config.js intro.spec.js

const path = require('path');
const os = require('os');
const fs = require('fs');
const { execFile } = require('child_process');
const { promisify } = require('util');
const { _electron } = require('playwright');
const { test, expect } = require('@playwright/test');

const { INTRO_TIMING } = require('../../electron/intro-timing');

const REPO_ROOT = path.resolve(__dirname, '../../../..');
const ELECTRON_BIN = path.join(REPO_ROOT, 'apps/minds/node_modules/.bin/electron');
const APP_JS = path.join(__dirname, 'intro-app.js');

function withUserDataDir(run) {
  const userDataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-intro-'));
  return run(userDataDir).finally(() => fs.rmSync(userDataDir, { recursive: true, force: true }));
}

async function runHiddenScenario(scenario) {
  const { stdout } = await withUserDataDir((userDataDir) =>
    promisify(execFile)(ELECTRON_BIN, [APP_JS, '--no-sandbox', `--user-data-dir=${userDataDir}`], {
      env: { ...process.env, INTRO_SCENARIO: scenario },
      timeout: 60_000,
    }),
  );
  const line = stdout.split('\n').find((candidate) => candidate.startsWith('INTRO_RESULT '));
  expect(line, `no INTRO_RESULT line in the harness output:\n${stdout}`).toBeTruthy();
  return JSON.parse(line.slice('INTRO_RESULT '.length));
}

test('a window hidden before the mark lands still reports the intro over once startup is ready', async () => {
  const result = await runHiddenScenario('hidden-before-ready');

  // Hidden, and the mark still not landed long after it would have in a painted
  // window.
  expect(result.beforeReady).toEqual({ visibility: 'hidden', hasArrived: false, isIntroFinished: false });
  // Answered off the cue itself, not off a throttled timer or a frame that
  // never comes.
  expect(result.introFinishedAt, 'the page never reported the intro over').not.toBeNull();
  expect(result.introFinishedAt - result.readyAt).toBeLessThan(1_000);
});

test('a window hidden after startup is ready, before the mark lands, reports the intro over', async () => {
  const result = await runHiddenScenario('hidden-after-ready');

  expect(result.beforeHide).toEqual({ visibility: 'visible', hasArrived: false, isIntroFinished: false });
  expect(result.introFinishedAt, 'the page never reported the intro over').not.toBeNull();
  expect(result.introFinishedAt - result.hiddenAt).toBeLessThan(1_000);
});

test('a visible window plays the film out and parks the mark before reporting the intro over', async () => {
  await withUserDataDir(async (userDataDir) => {
    const electronApp = await _electron.launch({
      executablePath: ELECTRON_BIN,
      args: [APP_JS, '--no-sandbox', `--user-data-dir=${userDataDir}`],
      timeout: 60_000,
    });
    try {
      const page = await electronApp.firstWindow();
      await page.waitForFunction(() => location.hash === '#intro' && hasArrived === true, null, { timeout: 15_000 });

      const readyAt = await electronApp.evaluate(({ BrowserWindow }) => {
        BrowserWindow.getAllWindows()[0].webContents.send('startup-ready', { park: true });
        return Date.now();
      });
      const introFinishedAt = () => electronApp.evaluate(() => globalThis.__introFinishedAt);
      await expect.poll(introFinishedAt, { timeout: 10_000 }).not.toBeNull();
      // The departure ran its course rather than being cut short: the travel
      // and the settle, at least.
      expect((await introFinishedAt()) - readyAt).toBeGreaterThanOrEqual(INTRO_TIMING.travelMs + INTRO_TIMING.settleMs);
      expect(await page.evaluate(() => document.body.classList.contains('parked'))).toBe(true);
    } finally {
      await electronApp.close().catch(() => {});
    }
  });
});
