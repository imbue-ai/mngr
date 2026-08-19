// macOS windowless-state regressions from #473 ("keep the app running when all
// windows close"), reported as #480-#483.
//
// #473 made "app alive with zero windows" a steady state on macOS. Four code
// paths still assumed a window always exists, and each one dead-ended the app:
// a startup failure left it unable to open any window ever again, a window
// closed mid-startup stranded it unauthenticated, a backend crash with nothing
// open went unreported and unrecoverable, and a minds:// deeplink was silently
// dropped.
//
// The shared invariant under test: with the app alive and no windows open,
// every "give me a window" request resolves to a window showing the app's
// REAL state -- home, still-starting, or the error screen and its Retry.
//
// These drive the real signed bundle's MAIN process through
// electronApplication.evaluate, which is how a dock activate, a window close,
// and an open-url delivery are reachable without a renderer. (#473's PR
// described this as impossible; it is not, and the absence of a test here is
// why all four shipped.)

const { execSync } = require('child_process');
const {
  test,
  expect,
  liveUrl,
  closeAllWindows,
  windowOpenedBy,
  emitActivate,
  emitOpenUrl,
  captureAppOutput,
} = require('./fixtures');

// Every behavior here is macOS-only by construction: elsewhere closing the
// last window quits the app, so "windowless" never happens.
test.skip(() => process.platform !== 'darwin', 'windowless app state is macOS-only');

const BACKEND_READY_RE = /\[startup\] Backend ready at (http:\/\/localhost:\d+)/;

// shell.html renders the error view only once main sends it an error-details
// payload, so a visible #retry-btn means "this window is showing the error
// takeover, with the backend-restart path attached".
const RETRY_BUTTON = '#retry-btn';

function killListenerOnPort(port) {
  // Resolved PIDs only, never a pattern kill: the runner hosts unrelated
  // processes (and, on the self-hosted mac, the user's own). lsof can name
  // more than one holder of a listening socket, and leaving any of them alive
  // would keep the port bound and defeat the test.
  const pids = execSync(`lsof -nP -iTCP:${port} -sTCP:LISTEN -t`, { encoding: 'utf-8' })
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
  expect(pids, `no listener found on port ${port}`).not.toHaveLength(0);
  for (const pid of pids) {
    expect(pid).toMatch(/^\d+$/);
    execSync(`kill -9 ${pid}`);
  }
  return pids;
}

test.describe('startup failure', () => {
  // Any startup failure reaches showErrorInAllWindows; an unsatisfiable uv
  // interpreter request is the deterministic way to cause one. `uv sync`
  // resolves the interpreter before it touches the network or the cache, so
  // `UV_PYTHON=3.99` fails env setup ("No interpreter found for Python 3.99 in
  // managed installations") in seconds on a warm or cold runner alike, leaving
  // the app on the error takeover with no backend URL -- the #480 state.
  //
  // The injected variable must be one the SIGNED BUNDLE cannot override: the
  // packaged build embeds a client.toml and a root_name and passes
  // `--config-file` explicitly, so MINDS_CLIENT_CONFIG_PATH / MINDS_ROOT_NAME
  // from the environment are inert here (see the note atop fixtures.js).
  // env-setup.js spawns uv with a plain `{ ...process.env }`, so UV_PYTHON
  // survives.
  test.use({ mindsAppEnv: { UV_PYTHON: '3.99' } });

  test('#480 the error window can be reopened from the dock after being closed', async ({ mindsApp }) => {
    const { app, mainWindow } = mindsApp;

    await expect(mainWindow.locator(RETRY_BUTTON)).toBeVisible({ timeout: 5 * 60 * 1000 });
    await closeAllWindows(app);

    // The regression: openOrFocusWindow() had no backend URL to load and no
    // window to fall back on, so it returned having done nothing -- leaving an
    // app inert in the dock and the app switcher until Cmd+Q. Three activates,
    // because the original report confirmed it stayed dead across repeats.
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      const reopened = await windowOpenedBy(app, () => emitActivate(app));
      await expect(
        reopened.locator(RETRY_BUTTON),
        `activate #${attempt} should reopen the error screen with its Retry button`,
      ).toBeVisible({ timeout: 60 * 1000 });
      await closeAllWindows(app);
    }
  });
});

test.describe('healthy app', () => {
  test('#483 a minds:// deeplink with no window open opens one', async ({ mindsApp }) => {
    const { app, pickContentWindow } = mindsApp;

    await pickContentWindow(app, { timeoutMs: 5 * 60 * 1000 });
    await closeAllWindows(app);

    // Bare minds:// is the browser sign-in flow's "Open app" link
    // (--success-redirect-url minds://). docs/desktop-app.md promises it
    // "opens/focuses the app"; with no window it was received and dropped,
    // and 'activate' does not cover it (application:openURLs: need not fire
    // applicationShouldHandleReopen:).
    const opened = await windowOpenedBy(app, () => emitOpenUrl(app, 'minds://'));
    await expect
      .poll(() => liveUrl(opened), { timeout: 60 * 1000 })
      .toMatch(/^http:\/\/localhost:\d+\//);
  });

  test('#482 a backend crash with no window open reopens to Retry, not the dead port', async ({ mindsApp }) => {
    const { app, pickContentWindow } = mindsApp;
    const output = captureAppOutput(app);

    await pickContentWindow(app, { timeoutMs: 5 * 60 * 1000 });
    const [, backendBaseUrl] = await output.waitForLine(BACKEND_READY_RE);
    const port = new URL(backendBaseUrl).port;

    await closeAllWindows(app);
    killListenerOnPort(port);

    // Pre-fix the exit handler was gated on `bundles.size > 0`, so this crash
    // was reported nowhere and the next window was loaded at the now-dead
    // port -- landing on "This screen failed to load", whose Reload button
    // only re-loaded that same dead port.
    await output.waitForLine(/\[backend\] exited unexpectedly/, { timeoutMs: 60 * 1000 });

    const reopened = await windowOpenedBy(app, () => emitActivate(app));
    await expect(reopened.locator(RETRY_BUTTON)).toBeVisible({ timeout: 60 * 1000 });
    expect(await liveUrl(reopened), 'must not navigate a fresh window at the dead backend').not.toContain(
      `localhost:${port}`,
    );
  });

  test('#481 closing the window during startup does not strand the app on /login', async ({ mindsApp }) => {
    const { app, mainWindow } = mindsApp;
    const output = captureAppOutput(app);

    // Close while the window is still the shell.html loading takeover -- the
    // whole point is that the startup sequence loses its window mid-flight.
    expect(await liveUrl(mainWindow)).toContain('shell.html');
    await closeAllWindows(app);

    // The one-time login code sat inside a guard on that window's survival,
    // and it is the ONLY place the code is consumed. Its absence was the
    // whole defect, so assert the hop itself, not just the landing.
    await output.waitForLine(BACKEND_READY_RE);
    await output.waitForLine(/\[startup\] Consuming one-time code via/, { timeoutMs: 60 * 1000 });

    const reopened = await windowOpenedBy(app, () => emitActivate(app));
    await expect
      .poll(() => liveUrl(reopened), { timeout: 2 * 60 * 1000 })
      .toMatch(/^http:\/\/localhost:\d+\//);
    expect(await liveUrl(reopened), 'a stranded app lands every window on /login').not.toContain('/login');
  });
});
