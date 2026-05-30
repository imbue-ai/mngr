// Headed-mode visual demo: drives the create form without submitting.
// Useful for confirming Playwright can click + fill the actual chrome
// window's form on the installed bundle. Doesn't depend on lima.

const { test, expect } = require('./fixtures');

const STEP_DELAY_MS = 500;

test('visible UI drive: navigate to Create, fill form (no submit)', async ({ mindsApp }) => {
  const { mainWindow } = mindsApp;

  // From the projects landing, click the Create link in the sidebar.
  await mainWindow.getByRole('link', { name: /^create$/i }).click();

  // Now the create form should mount in the main window.
  await expect(mainWindow.locator('#create-form')).toBeVisible({ timeout: 60 * 1000 });
  await mainWindow.waitForTimeout(STEP_DELAY_MS);

  await mainWindow.locator('#host_name').click();
  await mainWindow.locator('#host_name').fill(`demo-${Date.now().toString(36)}`);
  await mainWindow.waitForTimeout(STEP_DELAY_MS);

  await mainWindow.locator('#launch_mode').selectOption('LIMA');
  await mainWindow.waitForTimeout(STEP_DELAY_MS);

  await mainWindow.locator('#ai_provider').selectOption('API_KEY');
  await mainWindow.waitForTimeout(STEP_DELAY_MS);

  await mainWindow.locator('#anthropic_api_key').click();
  await mainWindow.locator('#anthropic_api_key').fill('sk-ant-demo-placeholder');
  await mainWindow.waitForTimeout(STEP_DELAY_MS);

  await mainWindow.locator('#toggle-advanced').click();
  await mainWindow.waitForTimeout(STEP_DELAY_MS);

  await mainWindow.locator('#git_url').click();
  await mainWindow.locator('#git_url')
    .fill('https://github.com/imbue-ai/forever-claude-template');
  await mainWindow.waitForTimeout(STEP_DELAY_MS);

  await mainWindow.locator('#branch').click();
  await mainWindow.locator('#branch').fill('pilot');

  // Stay on the filled form so the watcher can see end-state.
  await mainWindow.waitForTimeout(3 * 1000);
});
