// Playwright fixture: launches the installed /Applications/Minds.app.
//
// Note on isolation: the signed bundle's `getMindsRootName()` reads the
// baked-in `resources/pyproject/imbue/minds/config/envs/_bundled/root_name`
// file, which takes precedence over MINDS_ROOT_NAME from the environment
// (paths.js:148-164). So we cannot isolate state via env var alone for the
// shipped CEO build. Tests run against the user's live `~/.minds/` state.
// Specs are responsible for cleaning up any workspaces they create
// (`mngr destroy` or the destroy button) before exiting.
//
// To run cleanly, quit any user-launched minds.app first -- Playwright's
// `electron.launch()` will deadlock-exit silently on Electron's
// requestSingleInstanceLock if a prior Minds is still alive (we hit this
// in early iterations: PID 28024 lingered after Cmd-Q).

const path = require('path');
const fs = require('fs');
const { _electron: electron } = require('playwright');
const base = require('@playwright/test');

const DEFAULT_APP_PATH = '/Applications/Minds.app/Contents/MacOS/Minds';

// Each Minds window is a single web context now (the chrome page, which hosts
// hub pages, the sandboxed workspace iframe, and the in-DOM modals), so
// the user-facing UI is simply the window whose URL is on the backend origin --
// including `/workspace/<id>`, the route the page sits on while displaying a
// workspace. `_pick_content_page` in e2e_workspace_runner.py is the Python twin.
const _BACKEND_ORIGIN_RE = /^http:\/\/localhost:\d+(?:\/|$)/;

// The URL of the document currently in `page`, read from the document.
//
// `page.url()` is Playwright's own bookkeeping, updated from the CDP navigation
// events its session receives. main.js drives these WebContentsViews from the
// Electron MAIN process (`webContents.loadURL` / `loadFile`), and such a commit
// does not reliably reach an attached client: a view can sit on `/welcome`
// while Playwright still reports the `shell.html` it saw at attach time, for the
// rest of the run. The session stays healthy -- evaluating in the live document
// reports the real URL.
async function liveUrl(page) {
  try {
    return await page.evaluate(() => location.href);
  } catch {
    return page.url();
  }
}

async function pickContentWindow(app, { timeoutMs = 60 * 1000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = [];
  while (Date.now() < deadline) {
    const wins = app.windows();
    last = await Promise.all(wins.map(liveUrl));
    const idx = last.findIndex((u) => _BACKEND_ORIGIN_RE.test(u));
    if (idx !== -1) return wins[idx];
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(
    `No content window settled on a backend URL within ${timeoutMs}ms; observed: ${JSON.stringify(last)}`
  );
}

const test = base.test.extend({
  // Extra env for the launched app, layered over the runner's own. Override
  // per-file or per-describe with `test.use({ mindsAppEnv: { ... } })` -- how
  // macos-lifecycle.spec.js forces a deterministic startup failure.
  mindsAppEnv: [{}, { option: true }],

  mindsApp: async ({ mindsAppEnv }, use, testInfo) => {
    const execPath = process.env.MINDS_APP_PATH || DEFAULT_APP_PATH;
    if (!fs.existsSync(execPath)) {
      throw new Error(
        `minds.app binary not found at ${execPath}. Install it to /Applications/ or ` +
          `set MINDS_APP_PATH to a downloaded build.`
      );
    }

    const app = await electron.launch({
      executablePath: execPath,
      env: { ...process.env, ...mindsAppEnv },
      timeout: 5 * 60 * 1000,
    });

    const mainWindow = await app.firstWindow({ timeout: 5 * 60 * 1000 });

    await use({ app, mainWindow, pickContentWindow });

    // Save minds.log snapshot on failure for postmortem. Be defensive --
    // the outputDir may not exist if the test failed before any Playwright
    // assertion fired (e.g. fixture-level setup error).
    if (testInfo.status !== 'passed') {
      try {
        const mainLog = path.join(process.env.HOME, '.minds', 'logs', 'minds.log');
        if (fs.existsSync(mainLog)) {
          fs.mkdirSync(testInfo.outputDir, { recursive: true });
          const content = fs.readFileSync(mainLog, 'utf-8');
          const tail = content.split('\n').slice(-500).join('\n');
          fs.writeFileSync(path.join(testInfo.outputDir, 'minds.log.tail'), tail);
        }
      } catch (e) {
        console.error('[fixture] failed to capture minds.log:', e.message);
      }
    }

    // Teardown. macos-launch creates no mind, so no graceful shutdown is needed
    // (and SIGKILL never pops the native "Shut down running minds?" quit dialog
    // that a graceful `app.close()` would). Every step is hard-bounded so a cold
    // CI mac can't wedge teardown for the full test timeout.
    const { execSync } = require('child_process');
    const proc = app.process();
    try {
      proc.kill('SIGKILL');
    } catch (e) {
      console.error('[fixture] SIGKILL to minds app failed:', e.message);
    }
    // Unref this worker's stdio FIRST -- the app spawns detached helpers (minds
    // python backend, `mngr latchkey forward` in its own process group, crashpad)
    // that outlive the main process and keep the worker's inherited stdio sockets
    // ref'd, so the worker never exits ("worker did not exit ... force-killed it"
    // -> red job despite a passing test). Unref makes the worker exit regardless,
    // and runs before anything that could block so it always takes effect.
    try {
      process.stdout.unref();
      process.stderr.unref();
    } catch (e) {
      /* best effort */
    }
    // Reap those detached helpers (hygiene). BOUNDED: `execSync` has no default
    // timeout, and an unbounded `pkill` here once hung for the entire 600s test
    // timeout on a cold GHA mac. The `timeout` caps it. (macos-launch runs on an
    // ephemeral GHA Mac, so a broad minds-scoped pkill is safe.)
    try {
      execSync('pkill -9 -if "minds\\.app|/\\.minds/|mngr latchkey|mngr observe|Minds/Crashpad" 2>/dev/null || true', {
        stdio: 'ignore',
        timeout: 10000,
      });
    } catch (e) {
      /* best effort */
    }
    await Promise.race([
      app.close().catch(() => {}),
      new Promise((resolve) => setTimeout(resolve, 8000)),
    ]);
  },
});

// -- Lifecycle helpers (macos-lifecycle.spec.js) --
//
// These reach into the app's MAIN process via electronApplication.evaluate,
// which is what makes the windowless states testable at all: window closes and
// dock activations are main-process lifecycle events with no renderer to drive.

// Close every window the way the red traffic-light button does, and wait for
// main to settle on zero. Resolves the window count main itself sees, so a
// window that refuses to close (a quit-sequence interception) fails loudly
// rather than being papered over by a stale app.windows() snapshot.
async function closeAllWindows(app, { timeoutMs = 30 * 1000 } = {}) {
  await app.evaluate(({ BrowserWindow }) => {
    for (const win of BrowserWindow.getAllWindows()) win.close();
  });
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const count = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length);
    if (count === 0) return;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`Windows still open after ${timeoutMs}ms`);
}

// Fire a lifecycle event into main and return the window it opens. The
// listener is armed BEFORE the emit so a window that opens synchronously
// isn't missed.
async function windowOpenedBy(app, emit, { timeoutMs = 60 * 1000 } = {}) {
  const opened = app.waitForEvent('window', { timeout: timeoutMs });
  await emit();
  return opened;
}

// macOS dock-icon click (applicationShouldHandleReopen:).
function emitActivate(app) {
  return app.evaluate(({ app: electronApp }) => electronApp.emit('activate'));
}

// A minds:// URL delivered to an already-running app (application:openURLs:).
// main's handler calls event.preventDefault(), hence the stub event.
function emitOpenUrl(app, url) {
  return app.evaluate(({ app: electronApp }, deeplink) => {
    electronApp.emit('open-url', { preventDefault() {} }, deeplink);
  }, url);
}

// Buffer the app's console output (logger.js tees main-process console.* to
// stdout) so a test can wait on a startup milestone with no window to observe.
// Scoped to this launch, so a prior run's lines can't satisfy the wait.
function captureAppOutput(app) {
  let buffered = '';
  const proc = app.process();
  for (const stream of [proc.stdout, proc.stderr]) {
    if (stream) stream.on('data', (chunk) => { buffered += chunk.toString(); });
  }
  return {
    text: () => buffered,
    async waitForLine(pattern, { timeoutMs = 5 * 60 * 1000 } = {}) {
      const deadline = Date.now() + timeoutMs;
      while (Date.now() < deadline) {
        const match = buffered.match(pattern);
        if (match) return match;
        await new Promise((r) => setTimeout(r, 500));
      }
      throw new Error(`Never saw ${pattern} in the app's output within ${timeoutMs}ms`);
    },
  };
}

module.exports = {
  test,
  expect: base.expect,
  liveUrl,
  closeAllWindows,
  windowOpenedBy,
  emitActivate,
  emitOpenUrl,
  captureAppOutput,
};
