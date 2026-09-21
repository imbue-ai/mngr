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

/**
 * Run `updater.quitAndInstall()`, throwing the failure it reports instead of
 * returning quietly.
 *
 * electron-updater reports a failed install -- a cancelled pkexec prompt, a
 * dpkg error, a missing APPIMAGE -- only as an `error` event, emitted before
 * `quitAndInstall` returns (the Linux installers run their commands
 * synchronously), and then leaves the app running with the download still
 * staged. Without this the caller cannot tell a quit that is under way from
 * an app that stayed put.
 *
 * With `relaunch` (the 'app' side of `relaunchedBy`), the updater's own
 * restart is switched off and `relaunch` is called once the install has gone
 * through, before the quit the updater then asks for.
 */
function installStagedUpdate(updater, relaunch = null) {
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
  if (failure !== null) {
    throw new Error(`Installing the update failed: ${String((failure && failure.message) || failure)}`);
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
