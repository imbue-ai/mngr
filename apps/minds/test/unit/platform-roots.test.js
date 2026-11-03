// Unit tests for the shell's half of the platform-canonical path resolver.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/*.test.js)
//
// platform-roots.js is plain node (no Electron), so it is testable directly.
// Its Python counterpart is covered by imbue/minds/bootstrap_test.py; the two
// halves must name the same directories, which is what the app-name and tier
// assertions here pin down.
const test = require('node:test');
const assert = require('node:assert');
const path = require('path');

const {
  MINDS_APP_NAME,
  XDG_APP_DIR_NAME,
  STUDIO_ROOTS_OVERRIDE_ENV_VAR,
  tierForRootName,
  platformRootsFor,
  legacyDataDirFor,
} = require('../../electron/platform-roots');

const HOME = '/Users/tester';

function darwinRoots(rootName, env = {}) {
  return platformRootsFor({ rootName, platform: 'darwin', homeDir: HOME, env });
}

// -- the two app directory names --

test('MINDS_APP_NAME tracks the Electron productName', () => {
  // bootstrap.py pins its own copy against the same field, so anchoring both
  // halves here is what stops the shell writing a tier's state to one directory
  // while the backend reads another.
  assert.equal(MINDS_APP_NAME, require('../../package.json').productName);
});

test('XDG_APP_DIR_NAME tracks the package name and carries no space', () => {
  // The space in productName is what makes it wrong for a Linux data directory:
  // every path below these roots is interpolated into shell and command lines.
  assert.equal(XDG_APP_DIR_NAME, require('../../package.json').name);
  assert.ok(!/\s/.test(XDG_APP_DIR_NAME));
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

test('the XDG variables do not move the darwin roots', () => {
  // A developer's exported variables must not move the shell off the roots it
  // hardcodes, which is what the backend's own resolver is pinned against.
  assert.deepEqual(darwinRoots('minds', { XDG_DATA_HOME: '/d', XDG_CACHE_HOME: '/c', XDG_STATE_HOME: '/s' }), darwinRoots('minds'));
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

test('STUDIO_ROOTS_OVERRIDE collects all three roots under one directory', () => {
  // What keeps a CI run self-contained, off the roots the runner's own install owns.
  assert.deepEqual(
    platformRootsFor({
      rootName: 'minds-staging',
      platform: 'darwin',
      homeDir: HOME,
      env: { STUDIO_ROOTS_OVERRIDE: '/tmp/throwaway' },
    }),
    { state: '/tmp/throwaway/staging/state', cache: '/tmp/throwaway/staging/cache', logs: '/tmp/throwaway/staging/logs' }
  );
});

test('STUDIO_ROOTS_OVERRIDE outranks the XDG layout off darwin', () => {
  assert.deepEqual(
    platformRootsFor({
      rootName: 'minds',
      platform: 'linux',
      homeDir: HOME,
      env: {
        STUDIO_ROOTS_OVERRIDE: '/tmp/t',
        XDG_DATA_HOME: '/ignored/d',
        XDG_CACHE_HOME: '/ignored/c',
        XDG_STATE_HOME: '/ignored/s',
      },
    }),
    { state: '/tmp/t/production/state', cache: '/tmp/t/production/cache', logs: '/tmp/t/production/logs' }
  );
});

test('the retired override name raises rather than resolving the real roots', () => {
  assert.throws(
    () =>
      platformRootsFor({ rootName: 'minds', platform: 'linux', homeDir: HOME, env: { MINDS_DATA_HOME: '/tmp/t' } }),
    new RegExp(STUDIO_ROOTS_OVERRIDE_ENV_VAR)
  );
});

test('the retired name is ignored once the current one is set', () => {
  // Both set is what a shell looks like mid-rename, and the current name answers.
  assert.deepEqual(
    platformRootsFor({
      rootName: 'minds',
      platform: 'linux',
      homeDir: HOME,
      env: { MINDS_DATA_HOME: '/stale', STUDIO_ROOTS_OVERRIDE: '/tmp/t' },
    }),
    { state: '/tmp/t/production/state', cache: '/tmp/t/production/cache', logs: '/tmp/t/production/logs' }
  );
});

function linuxRoots(rootName, env = {}) {
  return platformRootsFor({ rootName, platform: 'linux', homeDir: HOME, env });
}

test('off darwin each role resolves to its own XDG base directory', () => {
  // Routing the cache to $XDG_CACHE_HOME is what gets the regenerable bytes
  // excluded: backup tools skip the cache directory, and nothing excluded the dotfolder.
  assert.deepEqual(linuxRoots('minds'), {
    state: `${HOME}/.local/share/${XDG_APP_DIR_NAME}/production`,
    cache: `${HOME}/.cache/${XDG_APP_DIR_NAME}/production`,
    logs: `${HOME}/.local/state/${XDG_APP_DIR_NAME}/production/log`,
  });
});

test('off darwin the tier is a segment below each role parent', () => {
  // The dotfolder carried the tier in its own name; here two tiers are two
  // subdirectories, so one tier's roots can never nest inside another's.
  const staging = linuxRoots('minds-staging');
  assert.equal(staging.state, `${HOME}/.local/share/${XDG_APP_DIR_NAME}/staging`);
  assert.notEqual(staging.state, linuxRoots('minds').state);
  assert.ok(!staging.state.startsWith(`${linuxRoots('minds').state}/`));
});

test('off darwin the XDG variables are honored', () => {
  assert.deepEqual(linuxRoots('minds', { XDG_DATA_HOME: '/d', XDG_CACHE_HOME: '/c', XDG_STATE_HOME: '/s' }), {
    state: `/d/${XDG_APP_DIR_NAME}/production`,
    cache: `/c/${XDG_APP_DIR_NAME}/production`,
    logs: `/s/${XDG_APP_DIR_NAME}/production/log`,
  });
});

test('an unset, empty or relative XDG variable falls back to its default', () => {
  // The spec reads empty as absent and requires a relative path to be ignored;
  // honoring one would put a tier's credentials somewhere relative to the cwd.
  assert.deepEqual(linuxRoots('minds', { XDG_DATA_HOME: '  ', XDG_CACHE_HOME: '', XDG_STATE_HOME: 'rel/ative' }), linuxRoots('minds'));
});

test('the XDG layout keeps every derived path under the role its parent names', () => {
  // Every derived path in paths.js hangs off these three. The virtualenv and
  // the managed CPython stay on state deliberately: regenerable, but the
  // backend cannot boot without them, and a cache root is purgeable.
  const { state, cache, logs } = linuxRoots('minds');
  const stateRoot = `${HOME}/.local/share/${XDG_APP_DIR_NAME}/production`;
  assert.equal(path.join(state, 'mngr'), `${stateRoot}/mngr`);
  assert.equal(path.join(state, 'latchkey'), `${stateRoot}/latchkey`);
  assert.equal(path.join(state, '.venv'), `${stateRoot}/.venv`);
  assert.equal(path.join(state, '.uv-python'), `${stateRoot}/.uv-python`);
  assert.equal(path.join(cache, '.uv-cache'), `${HOME}/.cache/${XDG_APP_DIR_NAME}/production/.uv-cache`);
  assert.equal(logs, `${HOME}/.local/state/${XDG_APP_DIR_NAME}/production/log`);
});

test('the legacy dotfolder is only where the migration reads from', () => {
  // Distinct from every live root, which is what gives a Linux launch a real
  // source and destination instead of the state-root-is-legacy-root short circuit.
  assert.equal(legacyDataDirFor({ rootName: 'minds', homeDir: HOME }), `${HOME}/.minds`);
  assert.notEqual(legacyDataDirFor({ rootName: 'minds', homeDir: HOME }), linuxRoots('minds').state);
});
