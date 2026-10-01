'use strict';

// How a downloaded update gets installed, per platform and package type.
//
// Deliberately free of any `electron` import so the table is unit-testable
// under plain node (see ../test/unit/install-policy.test.js).
//
// Two policies:
//   on-quit     The staged update installs when the app quits, or sooner
//               from the "Restart now" control. macOS: Squirrel swaps the
//               bundle silently, so quitting is a fine time to do it.
//   on-request  Nothing installs until the user asks. Linux: the .deb path
//               runs `dpkg -i` under pkexec, which raises the system password
//               prompt -- that only makes sense right after a click, never at
//               quit -- and the AppImage path replaces the running file, which
//               is worth doing only when asked.

const fs = require('fs');
const path = require('path');

// electron-builder writes this file into the resources dir of every deb, rpm
// and pacman package, and electron-updater picks its Linux implementation
// from it; an AppImage carries none.
const PACKAGE_TYPE_FILENAME = 'package-type';

/**
 * The Linux package type the running app was installed from: 'deb', 'rpm',
 * 'pacman', or 'appimage' when the marker file is absent or unreadable.
 */
function readLinuxPackageType(resourcesPath) {
  try {
    const raw = fs.readFileSync(path.join(resourcesPath, PACKAGE_TYPE_FILENAME), 'utf8').trim();
    return raw === '' ? 'appimage' : raw;
  } catch (err) {
    if (err && err.code !== 'ENOENT') {
      console.warn(`[update] Could not read ${PACKAGE_TYPE_FILENAME}: ${err.message}`);
    }
    return 'appimage';
  }
}

/**
 * The install policy for a platform and Linux package type.
 *
 * Returns `{ policy, needsPasswordToInstall, relaunchedBy }`. `relaunchedBy`
 * says who starts the new version after the install: 'updater' leaves it to
 * electron-updater (Squirrel on macOS), 'app' means the app starts it itself
 * once this process has exited (see linux-relaunch.js).
 */
function installPolicyFor({ platform, packageType }) {
  if (platform !== 'linux') {
    return { policy: 'on-quit', needsPasswordToInstall: false, relaunchedBy: 'updater' };
  }
  // Every package-manager-installed shape reinstalls through the system's
  // privileged package tool; only the AppImage can replace itself. Neither
  // restart can be left to electron-updater: the package installers use
  // Electron's app.relaunch, whose replacement carries no_new_privs and so
  // could never run the package tool under pkexec again, and the AppImage
  // installer starts the replaced file before this process has quit, where
  // it dies on the single-instance lock (see appimage-updater.js).
  return {
    policy: 'on-request',
    needsPasswordToInstall: packageType !== 'appimage',
    relaunchedBy: 'app',
  };
}

/**
 * What the app starts after an install, for `relaunchedBy: 'app'`.
 *
 * An AppImage install replaces the file, and the mounted executable this
 * process runs from is gone once it exits, so the file itself is started with
 * no arguments: its launcher adds what the executable needs. Every other
 * install keeps the executable in place, so it is started again with this
 * process's arguments, as `app.relaunch` would.
 */
function relaunchTargetFor({ packageType, appImagePath, executablePath, args }) {
  if (packageType !== 'appimage') return { executablePath, args };
  if (typeof appImagePath !== 'string' || appImagePath === '') {
    throw new Error('An AppImage install has no APPIMAGE path to restart from');
  }
  return { executablePath: appImagePath, args: [] };
}

/** How long an install gets to start before it counts as one that never did. */
const INSTALL_START_TIMEOUT_MS = 20_000;
const INSTALL_START_POLL_MS = 50;

/**
 * Run `updater.quitAndInstall()`, throwing unless the install starts.
 *
 * The verdict is `hasInstallStarted`, which the caller reads from the
 * `before-quit-for-update` the install raises on Electron's own updater --
 * the one signal every platform gives: Squirrel raises it as it swaps the
 * bundle, and electron-updater raises it once a Linux installer has run.
 *
 * An `error` event is not the verdict, because it does not mean the same
 * thing everywhere. A cancelled pkexec prompt reports one and installs
 * nothing; a macOS install asked for while Squirrel is still taking the zip
 * reports one too (`quitAndInstall` arms the install for when Squirrel has
 * it, then kicks Squirrel with a native call that answers "The command is
 * disabled and cannot be executed") and installs seconds later. Reading the
 * error as failure told that user their update had failed while it was
 * installing. What an error is good for is saying why an install that never
 * started did not.
 *
 * With `relaunch` (the 'app' side of `relaunchedBy`), the updater's own
 * restart is switched off and `relaunch` is called once the install has gone
 * through, before the quit the updater then asks for.
 */
async function installStagedUpdate(
  updater,
  {
    relaunch = null,
    hasInstallStarted,
    timeoutMs = INSTALL_START_TIMEOUT_MS,
    intervalMs = INSTALL_START_POLL_MS,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  } = {},
) {
  if (typeof hasInstallStarted !== 'function') {
    throw new Error('installStagedUpdate needs a hasInstallStarted to read the signal the install itself raises');
  }
  if (relaunch !== null) {
    updater.autoRunAppAfterInstall = false;
  }
  let failure = null;
  const onError = (error) => {
    failure = error;
  };
  updater.on('error', onError);
  try {
    updater.quitAndInstall();
  } finally {
    updater.removeListener('error', onError);
  }
  // Checked before the first sleep: a Linux installer runs inside
  // `quitAndInstall`, so its signal is already there and `relaunch` still
  // lands in the same tick as the install it follows.
  for (let waited = 0; !hasInstallStarted(); waited += intervalMs) {
    if (waited >= timeoutMs) {
      const cause = failure === null ? 'the updater never started installing it' : String(failure.message || failure);
      throw new Error(`Installing the update failed: ${cause}`);
    }
    await sleep(intervalMs);
  }
  if (relaunch !== null) {
    relaunch();
  }
}

module.exports = {
  PACKAGE_TYPE_FILENAME,
  readLinuxPackageType,
  installPolicyFor,
  relaunchTargetFor,
  installStagedUpdate,
};
