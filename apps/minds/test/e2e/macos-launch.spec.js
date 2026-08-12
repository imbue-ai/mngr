// Layer-0 smoke: confirm minds.app launches to a usable state.
//
// A successful launch can land on any of the SPA's cold-start routes:
//   - the machines list / home (a runner with prior auth state, like a
//     logged-in dev machine),
//   - the first-run welcome splash (vanilla macos-latest CI runner),
//   - the once-per-install error-reporting notice, or
//   - a restored workspace window (dev machine with saved session state).
//
// Any of these landings proves the cold-launch path completed: Electron
// came up, the Python backend bound its port, and a real SPA page rendered.
// Accept all of them; do NOT require a particular auth state.

const { test, expect } = require('./fixtures');

test('main window launches to a usable state (Create or Welcome)', async ({ mindsApp }, testInfo) => {
  const { mainWindow, app, pickContentWindow } = mindsApp;
  // Assert against the content window, not firstWindow(): firstWindow()
  // can return the SPA title-bar view (Projects / Home / Back /
  // Forward, no auth UI), which carries none of the landing elements.
  // pickContentWindow returns the view that renders the welcome splash or
  // projects home.
  let content;
  try {
    content = await pickContentWindow(app, { timeoutMs: 3 * 60 * 1000 });
    // Identify a usable landing by stable structural hooks, not visible
    // copy, so wording redesigns can't break this smoke test. Each hook only
    // renders on a successfully-loaded real SPA page (the persistent
    // titlebar #minds-titlebar deliberately does NOT count: it also renders
    // on the RouteError page, so it can't prove a good landing):
    //   #landing-minds-settings  the home page's fixed settings launcher
    //   #welcome-signup-btn      the first-run welcome splash (three-choice gate)
    //   #consent-continue        the error-reporting notice
    //   #content-frame           the workspace surface (restored session)
    const landingMarker = content.locator(
      '#landing-minds-settings, #welcome-signup-btn, #consent-continue, #content-frame'
    );
    await expect(landingMarker.first()).toBeVisible({ timeout: 2 * 60 * 1000 });
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
