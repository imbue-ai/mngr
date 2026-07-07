// Unit tests for the dev Electron launcher's pure helpers.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The launcher itself shells out to `open` / the electron binary (verified
// manually); these tests lock in the pure argv/bundle-path derivation, which is
// what makes the macOS foreground-app launch correct.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  macAppBundleForBinary,
  buildDarwinOpenArgs,
  findAppPidInPsLines,
} = require('../../scripts/launch_electron');

test('macAppBundleForBinary strips the macOS binary suffix to the .app bundle', () => {
  const binary = '/x/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron';
  assert.equal(macAppBundleForBinary(binary), '/x/node_modules/electron/dist/Electron.app');
});

test('macAppBundleForBinary returns null for a non-macOS binary path', () => {
  assert.equal(macAppBundleForBinary('/usr/lib/electron/electron'), null);
});

test('buildDarwinOpenArgs launches a new instance with app dir last', () => {
  const args = buildDarwinOpenArgs('/x/Electron.app', '/repo/apps/minds', {});
  assert.deepEqual(args, ['-n', '/x/Electron.app', '--args', '/repo/apps/minds']);
});

test('buildDarwinOpenArgs forwards every env var as a --env KEY=VALUE pair before --args', () => {
  const args = buildDarwinOpenArgs('/x/Electron.app', '/app', {
    MINDS_ROOT_NAME: 'minds-dev-xiaq',
    PATH: '/opt/bin:/usr/bin',
  });
  // Each var becomes an adjacent `--env NAME=VALUE` pair.
  const rootIdx = args.indexOf('--env');
  assert.ok(rootIdx !== -1);
  assert.ok(args.includes('MINDS_ROOT_NAME=minds-dev-xiaq'));
  assert.ok(args.includes('PATH=/opt/bin:/usr/bin'));
  // The app dir stays the final argument so `open` treats it as the app arg.
  assert.equal(args[args.length - 2], '--args');
  assert.equal(args[args.length - 1], '/app');
});

test('buildDarwinOpenArgs preserves env values containing spaces and equals signs', () => {
  const args = buildDarwinOpenArgs('/x/Electron.app', '/app', { FOO: 'a b=c d' });
  // A single argv element carries the whole value; no shell splitting.
  assert.ok(args.includes('FOO=a b=c d'));
});

test('findAppPidInPsLines picks the Electron main for the app dir, skipping helpers', () => {
  const appDir = '/repo/apps/minds';
  const lines = [
    '  501 /x/Electron.app/Contents/MacOS/Electron --type=gpu-process /repo/apps/minds',
    '  777 /x/Electron.app/Contents/MacOS/Electron /repo/apps/minds',
    '  888 /x/Electron.app/Contents/MacOS/Electron --type=renderer',
  ];
  assert.equal(findAppPidInPsLines(lines, appDir), 777);
});

test('findAppPidInPsLines returns null when no main process matches the app dir', () => {
  const lines = [
    '  501 /x/Electron.app/Contents/MacOS/Electron /other/app',
    '  888 /usr/bin/node scripts/launch_electron.js .',
  ];
  assert.equal(findAppPidInPsLines(lines, '/repo/apps/minds'), null);
});
