// Goal: drive an existing workspace's chat panel end-to-end via the actual UI.
//
// Scenario:
//   1. Launch minds.app (lands on projects list)
//   2. Click the configured workspace card
//   3. Wait for the chat panel iframe (`#content-frame`)
//   4. Type a prompt, send, wait for assistant reply containing expected substring
//
// Reuses an existing workspace (default `weishi30`) so we don't burn 5-15 min
// recreating one each run. Configure via env:
//   MINDS_WORKSPACE_NAME    -- the workspace name visible on the projects card
//   MINDS_PROMPT            -- the user message to send (default ping->pong)
//   MINDS_EXPECT_SUBSTRING  -- substring to assert in the assistant reply

const { test, expect } = require('./fixtures');

const WORKSPACE = process.env.MINDS_WORKSPACE_NAME || 'weishi30';
const PROMPT = process.env.MINDS_PROMPT || 'Reply with exactly the four characters: pong';
const EXPECT_SUBSTR = process.env.MINDS_EXPECT_SUBSTRING || 'pong';

test(`chat round-trip against existing workspace "${WORKSPACE}"`, async ({ mindsApp }) => {
  const { mainWindow } = mindsApp;

  // From the projects landing, find and click the workspace card.
  // The workspace title appears in card-shaped containers next to a
  // "Workspace settings" button (per the YAML accessibility tree we saw).
  // Click the text element to navigate into the chat.
  const workspaceTile = mainWindow.getByText(WORKSPACE, { exact: true }).first();
  await expect(workspaceTile).toBeVisible({ timeout: 2 * 60 * 1000 });
  await workspaceTile.click();

  // chrome.html mounts a top-level iframe #content-frame whose src is
  // proxied through mngr forward to the in-VM system_interface. Wait
  // for the iframe to attach and have a backend URL.
  const frame = mainWindow.locator('#content-frame');
  await expect(frame).toBeAttached({ timeout: 60 * 1000 });
  await expect.poll(async () => frame.evaluate((el) => el.src), {
    timeout: 60 * 1000,
  }).toMatch(/^http:\/\/(localhost|127\.0\.0\.1):\d+/);

  // Inside the iframe is the chat UI (served by system_interface). It
  // has a textarea (or contenteditable) for input, and renders assistant
  // messages into the DOM. Wait for the input to be available.
  const chatFrame = mainWindow.frameLocator('#content-frame');
  const input = chatFrame.locator('textarea, [contenteditable="true"]').first();
  await expect(input).toBeVisible({ timeout: 3 * 60 * 1000 });

  await input.click();
  await input.fill(PROMPT);
  await input.press('Enter');

  // Assistant reply containing the expected substring.
  await expect(chatFrame.getByText(EXPECT_SUBSTR, { exact: false }).first())
    .toBeVisible({ timeout: 5 * 60 * 1000 });
});
