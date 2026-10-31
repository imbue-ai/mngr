// Unit tests for the last-run version record behind the auto-update prompt's
// "first launch into an app update" trigger.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const launchVersion = require('../../electron/launch-version');

function tempDataDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'minds-launch-version-'));
}

/** One launch as main.js runs it: an install has run before when the session
 * state an earlier launch saved is there. */
function launch(dataDir, version) {
  return launchVersion.recordLaunchVersion(dataDir, version, {
    hasRunBefore: fs.existsSync(path.join(dataDir, 'window-state.json')),
  });
}

function recordedVersion(dataDir) {
  return JSON.parse(fs.readFileSync(path.join(dataDir, launchVersion.RECORD_FILENAME), 'utf8')).version;
}

test("a fresh install's first launch is not a launch into an update, and records its version", () => {
  const dataDir = tempDataDir();
  assert.deepEqual(launch(dataDir, '0.8.7'), { previousVersion: null, isFirstLaunchAfterUpdate: false });
  assert.equal(recordedVersion(dataDir), '0.8.7');
});

test('relaunching the same version is not a launch into an update', () => {
  const dataDir = tempDataDir();
  launch(dataDir, '0.8.7');
  assert.deepEqual(launch(dataDir, '0.8.7'), { previousVersion: '0.8.7', isFirstLaunchAfterUpdate: false });
});

test('the first launch into a new version says so, and only that launch does', () => {
  const dataDir = tempDataDir();
  launch(dataDir, '0.8.7');
  assert.deepEqual(launch(dataDir, '0.8.8'), { previousVersion: '0.8.7', isFirstLaunchAfterUpdate: true });
  assert.deepEqual(launch(dataDir, '0.8.8'), { previousVersion: '0.8.8', isFirstLaunchAfterUpdate: false });
});

test('an install upgrading from a build that kept no record counts as a launch into an update', () => {
  // The release that starts recording is itself an update for every existing
  // install; its earlier session state is what tells it from a fresh one.
  const dataDir = tempDataDir();
  fs.writeFileSync(path.join(dataDir, 'window-state.json'), JSON.stringify({ windows: [] }));
  assert.deepEqual(launch(dataDir, '0.8.8'), { previousVersion: null, isFirstLaunchAfterUpdate: true });
  assert.equal(recordedVersion(dataDir), '0.8.8');
});

test('a corrupt record is replaced, and read like a missing one', (t) => {
  const warn = t.mock.method(console, 'warn', () => {});
  const dataDir = tempDataDir();
  fs.writeFileSync(path.join(dataDir, 'window-state.json'), JSON.stringify({ windows: [] }));
  fs.writeFileSync(path.join(dataDir, launchVersion.RECORD_FILENAME), '{not json');

  assert.deepEqual(launch(dataDir, '0.8.9'), { previousVersion: null, isFirstLaunchAfterUpdate: true });
  assert.equal(recordedVersion(dataDir), '0.8.9');
  assert.match(String(warn.mock.calls[0].arguments[0]), /unreadable record/);
  assert.deepEqual(launch(dataDir, '0.8.9'), { previousVersion: '0.8.9', isFirstLaunchAfterUpdate: false });
});
