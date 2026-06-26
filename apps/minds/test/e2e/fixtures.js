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

// Minds' BaseWindow has multiple WebContentsViews; `firstWindow()` returns
// the chrome view (URL like `http://localhost:<port>/_chrome`) which only
// renders the title bar. The actual login / projects / chat UI lives on a
// sibling page on the same localhost origin without the `_chrome` prefix.
// `_pick_content_page` in e2e_workspace_runner.py is the Python twin.
const _BACKEND_ORIGIN_RE = /^http:\/\/localhost:\d+(?:\/|$)/;
const _CHROME_PATH_RE = /^http:\/\/localhost:\d+\/_chrome(?:\/|$|\?)/;

async function pickContentWindow(app, { timeoutMs = 60 * 1000 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let last = [];
  while (Date.now() < deadline) {
    last = app.windows().map((p) => p.url());
    const hit = app.windows().find((p) => {
      const u = p.url();
      return _BACKEND_ORIGIN_RE.test(u) && !_CHROME_PATH_RE.test(u);
    });
    if (hit) return hit;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error(
    `No content window settled on a backend URL within ${timeoutMs}ms; observed: ${JSON.stringify(last)}`
  );
}

const test = base.test.extend({
  mindsApp: async ({}, use, testInfo) => {
    const execPath = process.env.MINDS_APP_PATH || DEFAULT_APP_PATH;
    if (!fs.existsSync(execPath)) {
      throw new Error(
        `minds.app binary not found at ${execPath}. Install it to /Applications/ or ` +
          `set MINDS_APP_PATH to a downloaded build.`
      );
    }

    const app = await electron.launch({
      executablePath: execPath,
      env: { ...process.env },
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

module.exports = { test, expect: base.expect };
