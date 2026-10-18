// Unit tests for the background launch command an agent runs to start the app.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');

const {
  LAUNCH_ARGV_ENV_VAR,
  macBundlePathFromExecPath,
  backgroundLaunchArgv,
  backgroundLaunchEnv,
} = require('../../electron/background-launch');
const { detachedStartCommand } = require('../../electron/linux-relaunch');

// The argv `detachedStartCommand` produces for `executablePath --background`;
// the detachment itself is that command's contract (see linux-relaunch.test.js).
function detachedBackgroundArgv(executablePath) {
  const { command, args } = detachedStartCommand({ executablePath, args: ['--background'] });
  return [command, ...args];
}

test('a packaged macOS build launches its bundle through open, in the background', () => {
  assert.deepEqual(
    backgroundLaunchArgv({
      platform: 'darwin',
      isPackaged: true,
      execPath: '/Applications/Imbue Studio.app/Contents/MacOS/Imbue Studio',
      appImagePath: undefined,
    }),
    ['open', '-g', '-n', '-a', '/Applications/Imbue Studio.app', '--args', '--background'],
  );
});

test('a macOS executable outside a bundle records nothing', () => {
  assert.equal(
    backgroundLaunchArgv({ platform: 'darwin', isPackaged: true, execPath: '/usr/local/bin/electron', appImagePath: undefined }),
    null,
  );
  assert.equal(macBundlePathFromExecPath('/opt/x/Contents/MacOS/x'), null);
  assert.equal(macBundlePathFromExecPath('/Applications/X.app/Contents/Other/x'), null);
});

test('a packaged Linux build detaches the AppImage file when there is one, else the installed binary', () => {
  const fromAppImage = backgroundLaunchArgv({
    platform: 'linux',
    isPackaged: true,
    execPath: '/tmp/.mount_imbue1234/imbue-studio',
    appImagePath: '/home/alice/Downloads/Imbue Studio.AppImage',
  });
  assert.deepEqual(fromAppImage, detachedBackgroundArgv('/home/alice/Downloads/Imbue Studio.AppImage'));

  const fromDeb = backgroundLaunchArgv({
    platform: 'linux',
    isPackaged: true,
    execPath: '/opt/Imbue Studio/imbue-studio',
    appImagePath: '',
  });
  assert.deepEqual(fromDeb, detachedBackgroundArgv('/opt/Imbue Studio/imbue-studio'));
});

test('an unpackaged build and an unknown platform record nothing, so the env entry is absent', () => {
  assert.equal(
    backgroundLaunchArgv({ platform: 'darwin', isPackaged: false, execPath: '/x/Contents/MacOS/x', appImagePath: undefined }),
    null,
  );
  assert.equal(
    backgroundLaunchArgv({ platform: 'win32', isPackaged: true, execPath: 'C:\\x.exe', appImagePath: undefined }),
    null,
  );
  assert.deepEqual(
    backgroundLaunchEnv({ platform: 'darwin', isPackaged: false, execPath: '/x', appImagePath: undefined }),
    {},
  );
});

test('the env entry carries the argv as a JSON array', () => {
  const env = backgroundLaunchEnv({
    platform: 'darwin',
    isPackaged: true,
    execPath: '/Applications/Imbue Studio.app/Contents/MacOS/Imbue Studio',
    appImagePath: undefined,
  });
  assert.deepEqual(Object.keys(env), [LAUNCH_ARGV_ENV_VAR]);
  assert.deepEqual(JSON.parse(env[LAUNCH_ARGV_ENV_VAR]), [
    'open', '-g', '-n', '-a', '/Applications/Imbue Studio.app', '--args', '--background',
  ]);
});
