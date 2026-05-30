// Create a fresh LIMA workspace through the UI and round-trip a chat message.
// Matches the user-flow:
//   home -> Create -> show advanced -> name + branch=pilot + compute=lima
//   -> click Create button -> wait for workspace -> send first message
//
// Uses the existing AI provider default (subscription -- relies on the
// user being logged in to Claude on this machine). Configure name /
// branch / prompt via env if desired.

const { test, expect } = require('./fixtures');

const HOST_NAME = process.env.MINDS_TEST_HOST_NAME
  || `pw-${Date.now().toString(36)}`;
const BRANCH = process.env.MINDS_TEST_BRANCH || 'pilot';
const PROMPT = process.env.MINDS_TEST_PROMPT
  || 'Reply with exactly the four characters: pong';
const EXPECT_SUBSTR = process.env.MINDS_TEST_EXPECT || 'pong';

test('home -> create LIMA workspace -> first message round-trip', async ({ mindsApp }) => {
  test.setTimeout(20 * 60 * 1000); // 20 min: cold uv + lima boot + bootstrap + chat

  const { mainWindow } = mindsApp;

  // The minds.app session restores the last URL the user was on. Force
  // navigate to /create directly rather than trying to find a "Create"
  // link in the sidebar (which doesn't exist when we're already on a
  // detail page).
  await expect.poll(async () => {
    try {
      return await mainWindow.evaluate(() => location.href);
    } catch (_) {
      return '';
    }
  }, {
    timeout: 60 * 1000,
    message: 'main window never reported a URL (Electron still booting?)',
  }).toMatch(/^http:\/\/(localhost|127\.0\.0\.1):\d+/);

  // Get the current backend origin from window.location, then goto /create.
  const origin = await mainWindow.evaluate(() => location.origin);
  await mainWindow.goto(`${origin}/create`);

  // Wait for the form to mount.
  await expect(mainWindow.locator('#create-form'))
    .toBeVisible({ timeout: 60 * 1000 });

  // Reveal advanced options (where branch lives).
  await mainWindow.locator('#toggle-advanced').click();
  await mainWindow.waitForTimeout(300);

  // Workspace name.
  await mainWindow.locator('#host_name').click();
  await mainWindow.locator('#host_name').fill(HOST_NAME);

  // Branch.
  await mainWindow.locator('#branch').click();
  await mainWindow.locator('#branch').fill(BRANCH);

  // Compute provider = LIMA. AI provider stays at default (subscription).
  await mainWindow.locator('#launch_mode').selectOption('LIMA');

  // Submit.
  await mainWindow.locator('#create-submit').click();

  // Wait for the chat panel iframe (#content-frame) to attach and point
  // at a backend URL. mngr forward exposes the in-VM system_interface
  // and iframe.src updates once it's reachable.
  const frame = mainWindow.locator('#content-frame');
  await expect(frame).toBeAttached({ timeout: 15 * 60 * 1000 });
  await expect.poll(async () => frame.evaluate((el) => el.src), {
    timeout: 15 * 60 * 1000,
    message: 'chat panel iframe never got a backend URL',
  }).toMatch(/^http:\/\/(localhost|127\.0\.0\.1):\d+/);

  // Find chat input inside the iframe and send the prompt.
  const chatFrame = mainWindow.frameLocator('#content-frame');
  const input = chatFrame.locator('textarea, [contenteditable="true"]').first();
  await expect(input).toBeVisible({ timeout: 5 * 60 * 1000 });
  await input.click();
  await input.fill(PROMPT);
  await input.press('Enter');

  // Assistant reply contains the expected substring.
  await expect(chatFrame.getByText(EXPECT_SUBSTR, { exact: false }).first())
    .toBeVisible({ timeout: 5 * 60 * 1000 });
});
