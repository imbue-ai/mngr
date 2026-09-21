'use strict';

// Restoring Chromium's process sandbox on a Linux install whose launcher
// passed `--no-sandbox` it did not need.
//
// ToDesktop's `noSandbox: "probe"` desktop entry runs
// `unshare -Ur true || echo --no-sandbox` from an unconfined /bin/sh. On a
// distribution that restricts unprivileged user namespaces through AppArmor
// (Ubuntu 24.04 and later) that probe fails, so the flag is passed -- even
// though the .deb's own post-install script has loaded an AppArmor profile
// for the executable whose `userns` rule lets the executable itself create
// the namespaces Chromium's sandbox needs. The probe answers for the shell,
// not for the binary.
//
// So when this process finds the flag on its own command line but can see
// that a sandbox would work, it relaunches itself without the flag. The
// decision is pure and unit-tested under plain node (see
// ../test/unit/linux-sandbox.test.js); only the two readers touch the system.

const fs = require('fs');
const path = require('path');

const NO_SANDBOX_SWITCH = '--no-sandbox';
// Carried by the relaunched process so a launcher that somehow re-adds the
// flag can never make the app relaunch forever.
const RELAUNCHED_SWITCH = '--minds-sandbox-relaunched';
// This process's AppArmor label, e.g. `minds (unconfined)` under the profile
// electron-builder installs, or `unconfined` with none.
const APPARMOR_LABEL_PATH = '/proc/self/attr/current';
// Chromium's setuid sandbox helper, shipped beside the executable.
const SANDBOX_HELPER_FILENAME = 'chrome-sandbox';
// Chromium uses the helper only when it is root-owned, setuid, and
// world-executable (its error message asks for mode 4755). fs.constants has
// no S_ISUID.
const SETUID_BIT = 0o4000;
const WORLD_EXECUTABLE_BIT = fs.constants.S_IXOTH;

/**
 * How Chromium could sandbox its processes here, or null when it could not.
 *
 * `apparmorLabel` is the text of /proc/self/attr/current; `profileName` is the
 * profile electron-builder names after the executable (its basename). Only
 * the bare profile counts: `minds//&unconfined` is what `aa-exec` produces
 * and does not carry the profile's userns permission, and `unconfined` is
 * the launcher shell's own label. `sandboxHelperStat` is the helper's
 * `fs.Stats`, or null when it is absent.
 */
function sandboxAvailability({ apparmorLabel, profileName, sandboxHelperStat }) {
  if (sandboxHelperStat !== null && sandboxHelperStat !== undefined) {
    const isRootOwned = sandboxHelperStat.uid === 0;
    const isSetuid = (sandboxHelperStat.mode & SETUID_BIT) !== 0;
    const isWorldExecutable = (sandboxHelperStat.mode & WORLD_EXECUTABLE_BIT) !== 0;
    if (isRootOwned && isSetuid && isWorldExecutable) return 'setuid-helper';
  }
  if (typeof apparmorLabel === 'string') {
    const label = apparmorLabel.trim().split(/\s+/)[0];
    if (label === profileName) return 'apparmor-profile';
  }
  return null;
}

/**
 * Whether to relaunch without `--no-sandbox`, and with what arguments.
 *
 * `args` is the command line after the executable (process.argv.slice(1)).
 * `detectAvailability` returns what `sandboxAvailability` does and is called
 * only once a relaunch is otherwise on the table, so a launch that never
 * saw the flag reads nothing from the system.
 * Returns `{ action: 'relaunch', reason, args }` or `{ action: 'keep', reason }`.
 */
function decideSandboxRelaunch({ platform, isPackaged, args, detectAvailability }) {
  if (platform !== 'linux') return { action: 'keep', reason: 'not Linux' };
  if (!isPackaged) return { action: 'keep', reason: 'not a packaged build' };
  if (!args.includes(NO_SANDBOX_SWITCH)) return { action: 'keep', reason: 'the sandbox is on' };
  if (args.includes(RELAUNCHED_SWITCH)) {
    return { action: 'keep', reason: `already relaunched once and ${NO_SANDBOX_SWITCH} came back` };
  }
  const availability = detectAvailability();
  if (availability === null) return { action: 'keep', reason: 'no sandbox is available to this executable' };
  return {
    action: 'relaunch',
    reason: `${NO_SANDBOX_SWITCH} was passed but a sandbox is available (${availability})`,
    args: [...args.filter((arg) => arg !== NO_SANDBOX_SWITCH), RELAUNCHED_SWITCH],
  };
}

/** Whether this process is the relaunched one. */
function isSandboxRelaunched(args) {
  return args.includes(RELAUNCHED_SWITCH);
}

// A missing file is an ordinary answer; any other failure is logged so
// electron.log can say why no sandbox was found.
function readOrNull(filePath, read) {
  try {
    return read(filePath);
  } catch (err) {
    if (err.code !== 'ENOENT') console.warn(`[sandbox] Could not read ${filePath}: ${err.message}`);
    return null;
  }
}

function readApparmorLabel() {
  return readOrNull(APPARMOR_LABEL_PATH, (filePath) => fs.readFileSync(filePath, 'utf8'));
}

function readSandboxHelperStat(executablePath) {
  return readOrNull(path.join(path.dirname(executablePath), SANDBOX_HELPER_FILENAME), (filePath) => fs.statSync(filePath));
}

/** The sandbox this process could use, read from the running system. */
function detectSandboxAvailability(executablePath) {
  return sandboxAvailability({
    apparmorLabel: readApparmorLabel(),
    profileName: path.basename(executablePath),
    sandboxHelperStat: readSandboxHelperStat(executablePath),
  });
}

module.exports = {
  NO_SANDBOX_SWITCH,
  RELAUNCHED_SWITCH,
  sandboxAvailability,
  decideSandboxRelaunch,
  isSandboxRelaunched,
  detectSandboxAvailability,
};
