// Playwright config for end-to-end UI tests against minds.app.
//
// We target the installed `/Applications/Minds.app` by default so tests
// exercise the same signed bundle a user would run. Override via the
// MINDS_APP_PATH env var when iterating on a dev build or a downloaded
// pre-release artifact.
//
// Tests rely on the test runner spawning Electron via Playwright's
// `electron.launch`, which inherits this process's env. We isolate
// minds's host_dir under a per-run tmp path so a Playwright run
// neither destroys nor inherits the user's live `~/.minds/` state.

const path = require('path');

module.exports = {
  testDir: '.',
  // One worker only -- minds.app's Electron singleton + the bundled
  // mngr-forward port grabbing don't tolerate parallel instances.
  workers: 1,
  fullyParallel: false,
  // First-launch on a fresh isolated host_dir runs uv sync which can take
  // 30-60s before the chat panel is ready; lima boot to workspace ready
  // adds another 2-4 min.
  timeout: 10 * 60 * 1000,
  expect: { timeout: 30 * 1000 },
  retries: 0,
  reporter: [['list'], ['html', { outputFolder: 'test-results/playwright-html', open: 'never' }]],
  use: {
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
};
