// Layer-0 smoke: confirm minds.app's main window opens to a usable state.
//
// This is the test that runs on macos-latest (GitHub-hosted, no
// nested virtualization). It does not create a lima workspace, so no
// agent interaction is exercised; it just proves the Electron front-end
// reaches the chrome window and the Python backend bound a chat port.

const { test, expect } = require('./fixtures');

test('main window loads chrome with backend reachable', async ({ mindsApp }) => {
  const { mainWindow } = mindsApp;

  // The chrome (titlebar + iframe shell) loads from shell.html, then
  // main.js swaps the iframe's src to the Python backend URL once it's
  // listening. Wait for the iframe to be present and have a src like
  // http://localhost:<port>/ that resolves.
  const frame = mainWindow.locator('#content-frame');
  await expect(frame).toBeAttached({ timeout: 5 * 60 * 1000 });

  await expect.poll(async () => {
    return await frame.evaluate((el) => el.src);
  }, {
    message: 'iframe never pointed at a backend URL',
    timeout: 5 * 60 * 1000,
  }).toMatch(/^http:\/\/(localhost|127\.0\.0\.1):\d+/);

  // The chat-chrome /create page is the default landing for a fresh
  // install -- assert we can navigate to it and the create form mounts.
  const contentFrame = mainWindow.frameLocator('#content-frame');
  await expect(contentFrame.locator('#create-form')).toBeVisible({ timeout: 60 * 1000 });
  await expect(contentFrame.locator('#host_name')).toBeVisible();
  await expect(contentFrame.locator('#launch_mode')).toBeVisible();
  await expect(contentFrame.locator('#ai_provider')).toBeVisible();
  await expect(contentFrame.locator('#create-submit')).toBeVisible();
});
