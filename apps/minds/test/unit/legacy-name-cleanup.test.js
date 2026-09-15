'use strict';

// CLEANUP: delete alongside electron/legacy-name-cleanup.js once the "Mind"
// rename has been on stable long enough for installs to have launched once.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { removeLegacyNameDirs, legacyNameDirs } = require('../../electron/legacy-name-cleanup');

const MAC = { platform: 'darwin', homeDir: '/Users/someone', env: {} };
const LINUX = { platform: 'linux', homeDir: '/home/someone', env: {} };

test('macOS legacy dirs are the three Electron roots under the old name', () => {
  assert.deepEqual(legacyNameDirs(MAC), [
    '/Users/someone/Library/Application Support/Minds',
    '/Users/someone/Library/Logs/Minds',
    '/Users/someone/Library/Caches/Minds',
  ]);
});

test('Linux legacy dirs follow XDG, honoring overrides', () => {
  assert.deepEqual(legacyNameDirs(LINUX), ['/home/someone/.config/Minds', '/home/someone/.cache/Minds']);
  const overridden = { ...LINUX, env: { XDG_CONFIG_HOME: '/cfg', XDG_CACHE_HOME: '/cache' } };
  assert.deepEqual(legacyNameDirs(overridden), ['/cfg/Minds', '/cache/Minds']);
});

test('the updater cache is not a legacy directory', () => {
  // electron-builder derives updaterCacheDirName from the package name, which
  // the rename does not touch, so ~/Library/Caches/minds-updater is still live
  // and may hold a staged update.
  const dirs = legacyNameDirs(MAC);
  assert.ok(!dirs.some((dir) => dir.includes('minds-updater')), dirs.join(', '));
});

/** A throwaway HOME with the legacy directories populated. */
function makeHome() {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'legacy-name-cleanup-'));
  const environment = { platform: 'darwin', homeDir: home, env: {} };
  for (const dir of legacyNameDirs(environment)) {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'leftover'), 'x');
  }
  return { home, environment };
}

test('it removes the legacy directories and leaves nothing behind', () => {
  const { home, environment } = makeHome();
  try {
    const removed = removeLegacyNameDirs(environment);
    assert.equal(removed.length, 3);
    for (const dir of legacyNameDirs(environment)) {
      assert.ok(!fs.existsSync(dir), `${dir} survived`);
    }
    // No record of having run: the directories being gone is the record.
    assert.deepEqual(fs.readdirSync(path.join(home, 'Library')).sort(), [
      'Application Support',
      'Caches',
      'Logs',
    ]);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('running it again is a no-op', () => {
  const { home, environment } = makeHome();
  try {
    removeLegacyNameDirs(environment);
    assert.deepEqual(removeLegacyNameDirs(environment), []);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('the current data directory is never touched', () => {
  const { home, environment } = makeHome();
  const dataDir = path.join(home, '.minds');
  fs.mkdirSync(dataDir, { recursive: true });
  fs.writeFileSync(path.join(dataDir, 'device_id'), 'keep me');
  try {
    removeLegacyNameDirs(environment);
    assert.equal(fs.readFileSync(path.join(dataDir, 'device_id'), 'utf8'), 'keep me');
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
});
