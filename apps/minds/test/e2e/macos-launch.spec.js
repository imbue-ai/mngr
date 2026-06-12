// Layer-0 smoke: confirm minds.app launches to a usable state.
//
// A successful launch can land on either:
//   - the projects list / home (when the runner has prior auth state, like
//     a logged-in dev machine), where a "Create" link is visible, or
//   - the welcome / login splash (vanilla macos-latest CI runner with no
//     auth state), which renders a "Log In" link and a "Continue without
//     an account" button.
//
// Either landing proves the cold-launch path completed: Electron came up,
// the Python backend bound its port, and the chrome iframe rendered.
// Accept both; do NOT require a particular auth state.

const { test, expect } = require('./fixtures');

test('main window launches to a usable state (Create or Welcome)', async ({ mindsApp }, testInfo) => {
  const { mainWindow, app, pickContentWindow } = mindsApp;
  // Either auth path is fine -- racing them with `.or()` so we don't
  // burn a full timeout per state if the runner happens to be in the
  // logged-in one.
  const createLink = mainWindow.getByRole('link', { name: /^create$/i });
  const loginLink = mainWindow.getByRole('link', { name: /^log in$/i });
  const skipAccountBtn = mainWindow.getByRole('button', { name: /continue without an account/i });
  try {
    await expect(createLink.or(loginLink).or(skipAccountBtn).first())
      .toBeVisible({ timeout: 2 * 60 * 1000 });
  } finally {
    // Always attach a final main-window screenshot. The shared
    // playwright config defaults `screenshot: only-on-failure`, so a
    // passing run otherwise leaves nothing visual to inspect; a failing
    // run produces test-failed-*.png buried in test-results. `finally`
    // gives us both at zero extra branching, surfaced inline in the
    // html report.
    //
    // `mainWindow` is the chrome view (URL `/_chrome`) which renders
    // only the title bar -- screenshotting it gives a one-row strip.
    // Pick the content view (login form / projects landing) so the
    // attachment shows the actual app state. Fall back to chrome if
    // the content view hasn't appeared (cold-launch hang before
    // backend ready).
    let pageForShot = mainWindow;
    try {
      pageForShot = await pickContentWindow(app, { timeoutMs: 5 * 1000 });
    } catch (_) {
      // keep chrome fallback
    }
    const buf = await pageForShot.screenshot({ fullPage: true }).catch(() => null);
    if (buf) {
      await testInfo.attach('main-window-final', { body: buf, contentType: 'image/png' });
    }
  }
});
