'use strict';

// Starting a replacement for this process on Linux without app.relaunch.
//
// Electron's app.relaunch starts the replacement through a `--type=relauncher`
// helper that it launches with Chromium's base::LaunchProcess, which sets
// no_new_privs on its children by default; the replacement inherits that
// one-way flag and can never run a setuid helper again, so a .deb update's
// `pkexec dpkg -i` fails with "pkexec must be setuid root". (electron-updater's
// .deb installer restarts the app through app.relaunch, so an app that had
// updated once could never update again.) The browser process has no such
// flag and child_process.spawn adds none, so the replacement is spawned from
// here instead: a detached shell that waits for the old process to exit, so
// the replacement never runs alongside the process it replaces (which is
// still flushing electron.log), just as Electron's relauncher waits for its
// parent, and then execs the executable.
//
// The command is pure and unit-tested under plain node (see
// ../test/unit/linux-relaunch.test.js); only the spawner touches the system.

const { spawn } = require('child_process');

const RELAUNCH_SHELL = '/bin/sh';
const RELAUNCH_SCRIPT = 'while kill -0 "$1" 2>/dev/null; do sleep 0.1; done; shift; exec "$@"';
// nohup rather than setsid: POSIX, so present on every Linux the app runs on,
// and the hangup immunity plus the /dev/null stdio is what lets the started
// process outlive the terminal the command was typed into.
const DETACHED_START_SCRIPT = 'nohup "$@" </dev/null >/dev/null 2>&1 &';

/**
 * The command that starts `executablePath` with `args` once the process `pid`
 * has exited. Returns `{ command, args }` for child_process.spawn.
 */
function relaunchAfterExitCommand({ pid, executablePath, args }) {
  return {
    command: RELAUNCH_SHELL,
    args: ['-c', RELAUNCH_SCRIPT, 'minds-relaunch', String(pid), executablePath, ...args],
  };
}

/**
 * The command that starts `executablePath` with `args` detached from the
 * caller's session and returns at once. This is what an agent runs to bring
 * the app up in the background (the `launch.argv` the backend records in
 * `app.json`): a plain exec of the executable would tie the app to the
 * agent's shell, and a closed terminal would take it down.
 */
function detachedStartCommand({ executablePath, args }) {
  return {
    command: RELAUNCH_SHELL,
    args: ['-c', DETACHED_START_SCRIPT, 'imbue-studio-launch', executablePath, ...args],
  };
}

/**
 * Start `executablePath` with `args` once the process `pid` has exited, and
 * return the detached, unreferenced child that will do so (it emits 'spawn'
 * or 'error' on a later tick).
 *
 * Its stdio goes to /dev/null, where Electron's relauncher points its
 * replacement's too: the replacement is in its own session, so it outlives a
 * terminal the old process was launched from, and Node throws an uncaught EIO
 * on the first console write after that terminal closes. electron.log keeps
 * every console.* line either way.
 */
function startRelaunchAfterExit({ pid, executablePath, args }) {
  const { command, args: shellArgs } = relaunchAfterExitCommand({ pid, executablePath, args });
  const replacement = spawn(command, shellArgs, { detached: true, stdio: 'ignore' });
  replacement.unref();
  return replacement;
}

module.exports = { relaunchAfterExitCommand, startRelaunchAfterExit, detachedStartCommand };
