// Layer-1: drive the actual UI to create a LIMA workspace and round-trip
// a chat message. Requires a host with nested virtualization (so NOT
// macos-latest -- only the self-hosted minds-runner MacBook can run this).
//
// Env required: ANTHROPIC_API_KEY (used as the workspace's API_KEY provider).
// Env optional: MINDS_TEST_TEMPLATE_URL (default: imbue-ai/forever-claude-template),
//               MINDS_TEST_TEMPLATE_BRANCH (default: pilot).

const { test, expect } = require('./fixtures');

const TEMPLATE_URL = process.env.MINDS_TEST_TEMPLATE_URL
  || 'https://github.com/imbue-ai/forever-claude-template';
const TEMPLATE_BRANCH = process.env.MINDS_TEST_TEMPLATE_BRANCH || 'pilot';
const PROMPT = process.env.MINDS_TEST_PROMPT || 'Reply with exactly the four characters: pong';
const EXPECT_SUBSTR = process.env.MINDS_TEST_EXPECT || 'pong';

test('create LIMA workspace and round-trip first message', async ({ mindsApp }) => {
  const { mainWindow } = mindsApp;
  test.skip(!process.env.ANTHROPIC_API_KEY, 'ANTHROPIC_API_KEY not set');

  const frame = mainWindow.frameLocator('#content-frame');

  await expect(frame.locator('#create-form')).toBeVisible({ timeout: 5 * 60 * 1000 });

  const hostName = `pw-${Date.now().toString(36)}`;
  await frame.locator('#host_name').fill(hostName);
  await frame.locator('#launch_mode').selectOption('LIMA');
  await frame.locator('#ai_provider').selectOption('API_KEY');
  await frame.locator('#anthropic_api_key').fill(process.env.ANTHROPIC_API_KEY);

  // Open advanced options to set template url + branch.
  await frame.locator('#toggle-advanced').click();
  await frame.locator('#git_url').fill(TEMPLATE_URL);
  await frame.locator('#branch').fill(TEMPLATE_BRANCH);

  await frame.locator('#create-submit').click();

  // Creation page polls /api/create-agent/<id>/status; wait for the URL
  // to settle on the workspace's chat URL (frame.url() will reflect the
  // redirect once status==DONE).
  await expect.poll(async () => mainWindow.evaluate(() => location.href), {
    timeout: 15 * 60 * 1000,
    message: 'workspace did not reach DONE within 15 minutes',
  }).toMatch(new RegExp(`/[^/]+/${hostName}(/|$)`));

  // The chat panel is itself an iframe served from the in-VM
  // system_interface. Wait for its message input to appear and send the
  // first prompt.
  const chatFrame = mainWindow.frameLocator('#content-frame');
  const input = chatFrame.locator('textarea, [contenteditable=true]').first();
  await expect(input).toBeVisible({ timeout: 3 * 60 * 1000 });
  await input.fill(PROMPT);
  await input.press('Enter');

  // Wait for the assistant reply to contain EXPECT_SUBSTR.
  await expect(chatFrame.getByText(EXPECT_SUBSTR, { exact: false }))
    .toBeVisible({ timeout: 5 * 60 * 1000 });
});
