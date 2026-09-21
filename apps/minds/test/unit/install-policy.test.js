// Unit tests for the update install policy table.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { EventEmitter } = require('node:events');

const {
  installPolicyFor,
  installStagedUpdate,
  readLinuxPackageType,
  relaunchTargetFor,
} = require('../../electron/install-policy');

function tempResourcesDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'minds-install-policy-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test('macOS installs on quit and never asks for a password', () => {
  assert.deepEqual(installPolicyFor({ platform: 'darwin', packageType: 'appimage' }), {
    policy: 'on-quit',
    needsPasswordToInstall: false,
    relaunchedBy: 'updater',
  });
});

test('a Linux AppImage installs only on request, without a password, and restarts itself', () => {
  // electron-updater's AppImage restart starts the replaced file before this
  // process has quit, where it dies on the single-instance lock.
  assert.deepEqual(installPolicyFor({ platform: 'linux', packageType: 'appimage' }), {
    policy: 'on-request',
    needsPasswordToInstall: false,
    relaunchedBy: 'app',
  });
});

test('an AppImage restarts from the AppImage file, with no arguments of this process', () => {
  // The mounted executable this process runs from is gone once it exits, and
  // its --no-sandbox came from the file's own launcher, which adds it again.
  assert.deepEqual(
    relaunchTargetFor({
      packageType: 'appimage',
      appImagePath: '/home/alice/Apps/Mind.AppImage',
      executablePath: '/tmp/.mount_mindsXYZ/minds',
      args: ['--no-sandbox'],
    }),
    { executablePath: '/home/alice/Apps/Mind.AppImage', args: [] },
  );
});

test('an AppImage with no APPIMAGE path cannot be restarted from', () => {
  assert.throws(
    () => relaunchTargetFor({ packageType: 'appimage', appImagePath: null, executablePath: '/x', args: [] }),
    /no APPIMAGE path/,
  );
});

test('a .deb restarts its executable with the arguments this process was started with', () => {
  assert.deepEqual(
    relaunchTargetFor({
      packageType: 'deb',
      appImagePath: null,
      executablePath: '/opt/Mind/minds',
      args: ['--minds-sandbox-relaunched'],
    }),
    { executablePath: '/opt/Mind/minds', args: ['--minds-sandbox-relaunched'] },
  );
});

test('a Linux .deb installs only on request, warns about the password prompt, and restarts itself', () => {
  // dpkg runs under pkexec, so the prompt has to follow a click rather than a
  // quit the user thought was over; and the restart cannot be left to
  // electron-updater's app.relaunch, whose replacement could never run pkexec.
  assert.deepEqual(installPolicyFor({ platform: 'linux', packageType: 'deb' }), {
    policy: 'on-request',
    needsPasswordToInstall: true,
    relaunchedBy: 'app',
  });
});

test('the package type comes from the marker electron-builder writes', (t) => {
  const dir = tempResourcesDir(t);
  fs.writeFileSync(path.join(dir, 'package-type'), 'deb\n');
  assert.equal(readLinuxPackageType(dir), 'deb');
});

test('no marker means an AppImage, which ships none', (t) => {
  assert.equal(readLinuxPackageType(tempResourcesDir(t)), 'appimage');
});

test('an empty marker reads as an AppImage rather than as an empty package type', (t) => {
  const dir = tempResourcesDir(t);
  fs.writeFileSync(path.join(dir, 'package-type'), '\n');
  assert.equal(readLinuxPackageType(dir), 'appimage');
});

/**
 * An updater whose quitAndInstall reports `failure` the way electron-updater
 * does -- as an `error` event emitted before the call returns -- or, with no
 * failure, quits silently.
 */
function fakeUpdater(failure) {
  const updater = new EventEmitter();
  updater.installCalls = 0;
  updater.autoRunAppAfterInstall = true;
  updater.quitAndInstall = () => {
    updater.installCalls += 1;
    if (failure !== null) updater.emit('error', failure, String(failure.stack));
  };
  return updater;
}

test('an install electron-updater reports nothing about is a quit under way', () => {
  const updater = fakeUpdater(null);
  assert.doesNotThrow(() => installStagedUpdate(updater));
  assert.equal(updater.installCalls, 1);
});

test('an install electron-updater reports a failure for throws it, naming the cause', () => {
  // A cancelled pkexec prompt on a .deb: dpkg never ran, the app stays up, and
  // the only trace is the `error` event -- which the renderer has to hear about,
  // or the click did nothing as far as the user can see.
  const updater = fakeUpdater(new Error('Command failed: pkexec /bin/bash -c dpkg -i minds.deb'));
  assert.throws(() => installStagedUpdate(updater), /Installing the update failed: Command failed: pkexec/);
});

test('the failure listener does not outlive the install call', () => {
  // Left registered, every later updater error (a failed check) would be taken
  // as an install failure by the next call.
  const failing = fakeUpdater(new Error('dpkg refused'));
  assert.throws(() => installStagedUpdate(failing));
  assert.equal(failing.listenerCount('error'), 0);
  const quitting = fakeUpdater(null);
  installStagedUpdate(quitting);
  assert.equal(quitting.listenerCount('error'), 0);
});

test('with a relaunch of its own, the install turns off the updater restart and relaunches after installing', () => {
  const updater = fakeUpdater(null);
  const events = [];
  updater.quitAndInstall = () => events.push(`install autoRunAppAfterInstall=${updater.autoRunAppAfterInstall}`);
  installStagedUpdate(updater, () => events.push('relaunch'));
  assert.deepEqual(events, ['install autoRunAppAfterInstall=false', 'relaunch']);
});

test('without a relaunch of its own, the install leaves the updater restart alone', () => {
  const updater = fakeUpdater(null);
  installStagedUpdate(updater);
  assert.equal(updater.autoRunAppAfterInstall, true);
});

test('a failed install does not relaunch', () => {
  // The app stays up with the download staged; a restart armed here would
  // start the same old version the moment the user quit.
  const updater = fakeUpdater(new Error('dpkg refused'));
  let relaunches = 0;
  assert.throws(() => installStagedUpdate(updater, () => (relaunches += 1)));
  assert.equal(relaunches, 0);
});
