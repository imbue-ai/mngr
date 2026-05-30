// Layer-0 smoke: confirm minds.app launches to a usable state.
//
// Asserts the projects list landing page renders and the "Create" link
// is reachable. Does NOT navigate or create anything -- pure observability
// check. Safe to run anytime; ~30s on a warm venv, up to 2 min cold.

const { test, expect } = require('./fixtures');

test('main window loads projects landing with Create link', async ({ mindsApp }) => {
  const { mainWindow } = mindsApp;

  // The projects list / home shows a "Create" link in the sidebar (or
  // top-level if no projects exist). Either landing view is OK as long
  // as the link is reachable.
  await expect(mainWindow.getByRole('link', { name: /^create$/i }))
    .toBeVisible({ timeout: 5 * 60 * 1000 });
});
