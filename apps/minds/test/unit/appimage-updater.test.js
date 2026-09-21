// Unit tests for the AppImage updater that leaves the restart to the app.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test, mock } = require('node:test');
const assert = require('node:assert/strict');

const { AppImageUpdater } = require('electron-updater');

const { AppImageUpdaterWithoutRestart } = require('../../electron/appimage-updater');

// electron-updater's constructor reads only the version from the app adapter;
// a real one would require electron.
const FAKE_APP = { version: '0.6.1' };

test('the install runs electron-updater\'s file replacement on the run-after branch', (t) => {
  // The other branch runs the replaced file synchronously and waits for it
  // to exit; the run-after branch hands the start to spawnLog, which is
  // where the start is dropped.
  const seen = [];
  const doInstall = mock.method(AppImageUpdater.prototype, 'doInstall', function (options) {
    seen.push(options);
    return true;
  });
  t.after(() => doInstall.mock.restore());
  const updater = new AppImageUpdaterWithoutRestart(null, FAKE_APP);

  assert.equal(updater.doInstall({ isSilent: false, isForceRunAfter: false, isAdminRightsRequired: false }), true);
  assert.deepEqual(seen, [{ isSilent: false, isForceRunAfter: true, isAdminRightsRequired: false }]);
});

test('the start electron-updater would give the replacement does not happen', async () => {
  const updater = new AppImageUpdaterWithoutRestart(null, FAKE_APP);
  // Resolving true is what the real spawnLog reports for a started process,
  // and the AppImage installer ignores the value either way.
  assert.equal(await updater.spawnLog('/home/alice/Apps/Mind.AppImage', [], {}), true);
});
