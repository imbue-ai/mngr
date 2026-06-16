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
// the Python backend bound its port, and the content view rendered.
// Accept both; do NOT require a particular auth state.

const { test, expect } = require('./fixtures');

test('main window launches to a usable state (Create or Welcome)', async ({ mindsApp }, testInfo) => {
  const { mainWindow, app, pickContentWindow } = mindsApp;
  // Assert against the content window, not firstWindow(): firstWindow()
  // can return the `/_chrome` title-bar view (Projects / Home / Back /
  // Forward, no auth UI), which carries none of the landing elements.
  // pickContentWindow returns the view that renders the welcome splash or
  // projects home.
  let content;
  try {
    content = await pickContentWindow(app, { timeoutMs: 3 * 60 * 1000 });
    // Identify the splash by stable structural hooks (skip-account button
    // id / login link href), not visible copy or role, so a wording or
    // link-vs-button redesign can't break this smoke test. The logged-in
    // path shows a "Create" link.
    const welcomeSplash = content.locator('#skip-account-btn, a[href="/auth/login"]');
    const createLink = content.getByRole('link', { name: /^create$/i });
    await expect(welcomeSplash.or(createLink).first())
      .toBeVisible({ timeout: 2 * 60 * 1000 });
  } finally {
    // Attach a final screenshot of whichever page we resolved (content if
    // we got it, else the chrome fallback). The shared playwright config
    // defaults `screenshot: only-on-failure`, so this surfaces the actual
    // app state inline in the html report on both pass and fail.
    const pageForShot = content || mainWindow;
    const buf = await pageForShot.screenshot({ fullPage: true }).catch(() => null);
    if (buf) {
      await testInfo.attach('main-window-final', { body: buf, contentType: 'image/png' });
    }
  }
});
