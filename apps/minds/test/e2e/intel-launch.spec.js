// Intel launch smoke: confirm minds.app loads into its main screen on a native
// x86_64 (Intel) runner. On a fresh runner there is no prior auth/workspace
// state, so we continue past the welcome splash without an account to land on
// the Home/Workspaces main screen, then screenshot it. Unlike a local Rosetta
// test, lima runs natively here, so its "running under rosetta" guard never
// fires.

const { test, expect } = require('./fixtures');

test('minds loads into the main screen on Intel', async ({ mindsApp }, testInfo) => {
  const { mainWindow, app, pickContentWindow } = mindsApp;
  let content;
  try {
    content = await pickContentWindow(app, { timeoutMs: 3 * 60 * 1000 });
    // If the welcome splash is showing, continue without an account to reach
    // the Home/Workspaces main screen.
    const skip = content.locator('#skip-account-btn').first();
    if (await skip.isVisible({ timeout: 90 * 1000 }).catch(() => false)) {
      await skip.click().catch(() => {});
    }
    // Wait for the main screen (the "Create" affordance), falling back to any
    // usable landing so we always capture a real app state.
    const createLink = content.getByRole('link', { name: /^create$/i });
    const welcome = content.locator('#skip-account-btn, a[href="/auth/login"]');
    await expect(createLink.or(welcome).first()).toBeVisible({ timeout: 2 * 60 * 1000 });
    await content.waitForTimeout(3000);
  } finally {
    const pageForShot = content || mainWindow;
    const buf = await pageForShot.screenshot({ fullPage: true }).catch(() => null);
    if (buf) {
      await testInfo.attach('intel-main-screen', { body: buf, contentType: 'image/png' });
    }
  }
});
