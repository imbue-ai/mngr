// Layer-0 smoke: confirm minds.app launches to a usable state.
//
// A successful launch can land on either:
//   - the projects list / home (when the runner has prior auth state, like
//     a logged-in dev machine), where a "Create" link is visible, or
//   - the login screen (vanilla macos-latest CI runner with no auth state),
//     where a "Log in" button is visible.
//
// Either landing proves the cold-launch path completed: Electron came up,
// the Python backend bound its port, and the chrome iframe rendered.
// Accept both; do NOT require a particular auth state.

const { test, expect } = require('./fixtures');

test('main window launches to a usable state (Create or Log in)', async ({ mindsApp }) => {
  const { mainWindow } = mindsApp;
  // Either auth path is fine -- racing both with `.or()` so we don't
  // burn a full timeout per state if the runner happens to be in the
  // logged-in one.
  const createLink = mainWindow.getByRole('link', { name: /^create$/i });
  const loginBtn = mainWindow.getByRole('button', { name: /^log in$/i });
  await expect(createLink.or(loginBtn).first())
    .toBeVisible({ timeout: 2 * 60 * 1000 });
});
