// Unit tests for the shell's half of the platform-canonical path resolver.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/*.test.js)
//
// platform-roots.js is plain node (no Electron), so it is testable directly.
// Its Python counterpart is covered by imbue/minds/bootstrap_test.py; the two
// halves must name the same directories, which is what the MINDS_APP_NAME and
// tier assertions here pin down.
const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const { MINDS_APP_NAME, tierForRootName, platformRootsFor } = require('../../electron/platform-roots');

const HOME = '/Users/tester';

function darwinRoots(rootName) {
  return platformRootsFor({ rootName, platform: 'darwin', homeDir: HOME, dataHome: undefined });
}

// -- MINDS_APP_NAME --

test('MINDS_APP_NAME tracks the Electron productName', () => {
  // bootstrap.py pins its own copy against the same field, so anchoring both
  // halves here is what stops the shell writing a tier's state to one directory
  // while the backend reads another.
  assert.equal(MINDS_APP_NAME, require('../../package.json').productName);
});

// -- tierForRootName --

test('production has no suffix', () => {
  assert.equal(tierForRootName('minds'), 'production');
});

test('every other root name yields its suffix as the tier', () => {
  assert.equal(tierForRootName('minds-staging'), 'staging');
  assert.equal(tierForRootName('minds-dev-josh-3'), 'dev-josh-3');
  assert.equal(tierForRootName('minds-ci-20260518t140212z-abcd'), 'ci-20260518t140212z-abcd');
});

test('minds-staging and minds-dev-staging are different tiers', () => {
  // Distinguished today only by the folder-name suffix in $HOME; a
  // prefix-stripping slip while lifting the tier into a subdirectory would
  // silently merge a developer's env into shared staging.
  assert.notEqual(tierForRootName('minds-staging'), tierForRootName('minds-dev-staging'));
});

test('a root name outside the minds prefix raises', () => {
  assert.throws(() => tierForRootName('devminds'), /expected 'minds' or 'minds-<env-name>'/);
});

// -- platformRootsFor --

test('darwin resolves the three Apple-canonical roots, tier-suffixed', () => {
  // Caches and Logs are the roots Time Machine excludes by policy, so filing
  // regenerable data under them is what keeps it out of backups.
  assert.deepEqual(darwinRoots('minds'), {
    state: `${HOME}/Library/Application Support/${MINDS_APP_NAME}/production`,
    cache: `${HOME}/Library/Caches/${MINDS_APP_NAME}/production`,
    logs: `${HOME}/Library/Logs/${MINDS_APP_NAME}/production`,
  });
});

test('the three darwin roots stay distinct from each other', () => {
  const { state, cache, logs } = darwinRoots('minds');
  assert.equal(new Set([state, cache, logs]).size, 3);
});

test('tiers do not share a directory', () => {
  const staging = darwinRoots('minds-staging');
  const devStaging = darwinRoots('minds-dev-staging');
  assert.notEqual(staging.state, devStaging.state);
  assert.notEqual(staging.cache, devStaging.cache);
  assert.notEqual(staging.logs, devStaging.logs);
});

test('MINDS_DATA_HOME collects all three roots under one directory', () => {
  // What keeps CI runs self-contained and mac-runner-reset.sh a single rm -rf.
  assert.deepEqual(
    platformRootsFor({ rootName: 'minds-staging', platform: 'darwin', homeDir: HOME, dataHome: '/tmp/throwaway' }),
    { state: '/tmp/throwaway/staging/state', cache: '/tmp/throwaway/staging/cache', logs: '/tmp/throwaway/staging/logs' }
  );
});

test('MINDS_DATA_HOME outranks the dotfolder layout off darwin', () => {
  assert.deepEqual(
    platformRootsFor({ rootName: 'minds', platform: 'linux', homeDir: HOME, dataHome: '/tmp/t' }),
    { state: '/tmp/t/production/state', cache: '/tmp/t/production/cache', logs: '/tmp/t/production/logs' }
  );
});

test('off darwin all three roles live in the one dotfolder', () => {
  assert.deepEqual(platformRootsFor({ rootName: 'minds', platform: 'linux', homeDir: HOME, dataHome: undefined }), {
    state: `${HOME}/.minds`,
    cache: `${HOME}/.minds`,
    logs: `${HOME}/.minds/logs`,
  });
});

test('off darwin the tier stays in the dotfolder name', () => {
  // Where the Apple layout puts the tier one level below the root, the
  // dotfolder carries it, so two tiers are two dotfolders.
  const staging = platformRootsFor({ rootName: 'minds-staging', platform: 'linux', homeDir: HOME, dataHome: undefined });
  assert.equal(staging.state, `${HOME}/.minds-staging`);
  assert.notEqual(staging.state, platformRootsFor({ rootName: 'minds', platform: 'linux', homeDir: HOME, dataHome: undefined }).state);
});

test('the dotfolder layout matches what the app read before the move', () => {
  // Every derived path in paths.js hangs off these three, so reproducing them
  // exactly is what leaves a non-macOS install untouched by this change.
  const { state, cache, logs } = platformRootsFor({ rootName: 'minds', platform: 'linux', homeDir: HOME, dataHome: undefined });
  assert.equal(path.join(state, 'mngr'), `${HOME}/.minds/mngr`);
  assert.equal(path.join(state, 'latchkey'), `${HOME}/.minds/latchkey`);
  assert.equal(path.join(state, '.venv'), `${HOME}/.minds/.venv`);
  assert.equal(path.join(state, '.uv-python'), `${HOME}/.minds/.uv-python`);
  assert.equal(path.join(cache, '.uv-cache'), `${HOME}/.minds/.uv-cache`);
  assert.equal(logs, `${HOME}/.minds/logs`);
});
