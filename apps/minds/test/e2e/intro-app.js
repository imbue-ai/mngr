// Minimal Electron entry for the loading document's intro e2e (intro.spec.js):
// one window on the shipped shell.html#intro with the shipped preload, as
// electron/main.js's runStartupSequence opens it, and the `intro-finished`
// handshake recorded where the spec can read it. The intro is page-side and
// backend-independent, so no backend is booted; the harness or the spec plays
// main's part by sending `startup-ready`.
//
// With INTRO_SCENARIO set to one of HIDDEN_SCENARIOS this process drives the
// scenario itself and prints one `INTRO_RESULT <json>` line before exiting. It
// cannot be driven from Playwright: a DevTools client attached to the page
// keeps it painted and `visible` however hidden its window is, which is the one
// condition these scenarios are about.
const path = require('path');
const { app, BrowserWindow, ipcMain } = require('electron');

const ELECTRON_DIR = path.join(__dirname, '../../electron');

globalThis.__introFinishedAt = null;
ipcMain.on('intro-finished', () => {
  if (globalThis.__introFinishedAt === null) globalThis.__introFinishedAt = Date.now();
});

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Whether `check` came true before `deadline` (an epoch-ms instant).
async function waitUntil(check, deadline, intervalMs) {
  while (!(await check())) {
    if (Date.now() >= deadline) return false;
    await sleep(intervalMs);
  }
  return true;
}

const readIntroState = (win) =>
  win.webContents.executeJavaScript(
    '({ visibility: document.visibilityState, hasArrived, isIntroFinished })',
  );

async function reportIntroFinished(since, fields) {
  await waitUntil(() => globalThis.__introFinishedAt !== null, since + 5_000, 50);
  const result = { ...fields, introFinishedAt: globalThis.__introFinishedAt };
  process.stdout.write(`INTRO_RESULT ${JSON.stringify(result)}\n`, () => app.exit(0));
}

// Both start inside the arrival's empty lead (intro-pop.js TUNING.lead), so the
// mark cannot land before the window is hidden and stops painting.
const HIDDEN_SCENARIOS = {
  // Held hidden for longer than the whole arrival takes in a painted window,
  // then told startup is ready.
  'hidden-before-ready': async (win) => {
    win.hide();
    await sleep(4_000);
    const beforeReady = await readIntroState(win);
    const readyAt = Date.now();
    win.webContents.send('startup-ready', { park: true });
    await reportIntroFinished(readyAt, { beforeReady, readyAt });
  },
  // Told startup is ready while visible, so the departure is only due once the
  // mark lands, then hidden.
  'hidden-after-ready': async (win) => {
    win.webContents.send('startup-ready', { park: true });
    const isReady = () => win.webContents.executeJavaScript('isStartupReady');
    if (!(await waitUntil(isReady, Date.now() + 5_000, 10))) {
      throw new Error('the page never took the startup-ready cue');
    }
    const beforeHide = await readIntroState(win);
    const hiddenAt = Date.now();
    win.hide();
    await reportIntroFinished(hiddenAt, { beforeHide, hiddenAt });
  },
};

app.whenReady().then(async () => {
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    show: true,
    webPreferences: {
      preload: path.join(ELECTRON_DIR, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  await win.loadFile(path.join(ELECTRON_DIR, 'shell.html'), { hash: 'intro' });
  const scenario = process.env.INTRO_SCENARIO;
  if (scenario === undefined) return;
  if (!Object.hasOwn(HIDDEN_SCENARIOS, scenario)) throw new Error(`unknown INTRO_SCENARIO ${scenario}`);
  await HIDDEN_SCENARIOS[scenario](win);
}).catch((err) => {
  console.error('[intro-app]', err);
  app.exit(1);
});
