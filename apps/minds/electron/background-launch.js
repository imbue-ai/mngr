'use strict';

// The command that starts this install in background mode, detached from
// whoever runs it. The backend records it in the tier's `app.json` as
// `launch.argv`, so an agent that finds the app stopped can bring it up (see
// specs/imbue-studio-agent-access/spec.md, "Launching the app from an agent").
//
// Free of any `electron` import so it is unit-testable under plain node (see
// ../test/unit/background-launch.test.js); backend.js hands it the live values.

const path = require('path');

const { BACKGROUND_SWITCH } = require('./lifecycle-policy');
const { detachedStartCommand } = require('./linux-relaunch');

// The environment variable the backend reads the command from, as a JSON
// array of strings.
const LAUNCH_ARGV_ENV_VAR = 'MINDS_APP_LAUNCH_ARGV';

/**
 * The `.app` bundle an executable at `<bundle>/Contents/MacOS/<name>` belongs
 * to, or null when the path is not laid out that way (a dev run of the bare
 * electron binary, say).
 */
function macBundlePathFromExecPath(execPath) {
  const macosDir = path.dirname(execPath);
  const contentsDir = path.dirname(macosDir);
  const bundleDir = path.dirname(contentsDir);
  if (path.basename(macosDir) !== 'MacOS' || path.basename(contentsDir) !== 'Contents') return null;
  if (!bundleDir.endsWith('.app')) return null;
  return bundleDir;
}

/**
 * The argv that starts this install in the background, or null when there is
 * nothing stable to record: an unpackaged (dev) build, or a platform without a
 * launcher.
 *
 * macOS goes through `open`, which hands the launch to LaunchServices and
 * returns, so the app is never a child of the caller; `-g` keeps it from
 * taking the foreground, and `-n` starts a fresh instance even while one is
 * running, so the launch always reaches the running app through the
 * single-instance lock (whose `second-instance` ignores `--background`).
 * Without `-n`, `open` of a running app sends it a reopen event, which
 * Electron surfaces as `activate`, and that opens a window when none is open.
 * Linux goes through the detached shell form the
 * relaunch paths already use, around the AppImage file when there is one
 * (the mounted executable is gone once that process exits) and the installed
 * binary otherwise.
 */
function backgroundLaunchArgv({ platform, isPackaged, execPath, appImagePath }) {
  if (!isPackaged) return null;
  if (platform === 'darwin') {
    const bundlePath = macBundlePathFromExecPath(execPath);
    if (bundlePath === null) return null;
    return ['open', '-g', '-n', '-a', bundlePath, '--args', BACKGROUND_SWITCH];
  }
  if (platform === 'linux') {
    const executablePath = appImagePath && appImagePath.trim() !== '' ? appImagePath : execPath;
    const { command, args } = detachedStartCommand({ executablePath, args: [BACKGROUND_SWITCH] });
    return [command, ...args];
  }
  return null;
}

/**
 * The environment entry backend.js adds for the backend: the launch argv as
 * JSON, or nothing when there is no argv to record.
 */
function backgroundLaunchEnv(options) {
  const argv = backgroundLaunchArgv(options);
  if (argv === null) return {};
  return { [LAUNCH_ARGV_ENV_VAR]: JSON.stringify(argv) };
}

module.exports = {
  LAUNCH_ARGV_ENV_VAR,
  macBundlePathFromExecPath,
  backgroundLaunchArgv,
  backgroundLaunchEnv,
};
