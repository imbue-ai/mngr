#!/usr/bin/env node
// Dev-mode Electron launcher.
//
// On macOS, executing the Electron Mach-O binary directly (what `electron .`
// does) does NOT register the process as a proper foreground GUI app: recent
// macOS draws its window but gives it no Dock tile and never lets it become the
// key window, so it takes no keyboard input. Launching the SAME .app bundle
// through LaunchServices (`open`) fixes this -- the app becomes a normal
// foreground app. `open` does not inherit the caller's environment, so we
// forward every variable explicitly via `--env` (the dev app + its Python
// backend need the activated MINDS_*/MNGR_*/PATH values).
//
// LaunchServices reparents the app to launchd, OUTSIDE this launcher's process
// tree, so we cannot rely on child-process lifetime. Instead this launcher
// supervises: it locates the launched app process, forwards SIGTERM/SIGINT to
// it (so `concurrently -k` / `just minds-stop` / Ctrl-C stop the app cleanly),
// and stays alive until the app exits -- keeping the surrounding
// `concurrently -k` treating the Electron slot (and the CSS watcher beside it)
// as running for the app's lifetime.
//
// On every other platform, `electron .` already yields a first-class window, so
// we exec the binary directly and preserve the existing behavior.

const { spawn, execFileSync } = require('child_process');
const path = require('path');
const fs = require('fs');

// The `electron` npm package exports the path to its binary when required in a
// plain Node context (e.g. ".../dist/Electron.app/Contents/MacOS/Electron").
const electronBinary = require('electron');

const MACOS_BINARY_SUFFIX = '/Contents/MacOS/Electron';

/**
 * Resolve the `.app` bundle that contains a macOS Electron binary, or null when
 * the path is not a bundled macOS binary.
 */
function macAppBundleForBinary(binaryPath) {
  if (!binaryPath.endsWith(MACOS_BINARY_SUFFIX)) return null;
  return binaryPath.slice(0, -MACOS_BINARY_SUFFIX.length);
}

/**
 * Build the argv for `open` that launches the Electron app bundle as a new
 * foreground instance with the given app directory and forwarded environment.
 * Pure (no side effects) so it can be unit-tested.
 */
function buildDarwinOpenArgs(bundlePath, appDir, env) {
  const args = ['-n', bundlePath];
  for (const key of Object.keys(env)) {
    args.push('--env', `${key}=${env[key]}`);
  }
  args.push('--args', appDir);
  return args;
}

/**
 * Pick the Electron *main* process for a given app directory out of `ps`-style
 * "<pid> <command>" lines: its command runs the Electron binary with the app
 * dir as an argument and is not a `--type=` helper subprocess. Returns the pid
 * (number) or null. Pure so it can be unit-tested.
 */
function findAppPidInPsLines(lines, appDir) {
  for (const line of lines) {
    const match = line.match(/^\s*(\d+)\s+(.*)$/);
    if (!match) continue;
    const pid = Number(match[1]);
    const cmd = match[2];
    if (cmd.includes(MACOS_BINARY_SUFFIX) && cmd.includes(appDir) && !cmd.includes('--type=')) {
      return pid;
    }
  }
  return null;
}

function currentAppPid(appDir) {
  let out;
  try {
    out = execFileSync('ps', ['-Ao', 'pid=,command='], { encoding: 'utf8' });
  } catch {
    return null;
  }
  return findAppPidInPsLines(out.split('\n'), appDir);
}

const sleep = (ms) => new Promise((res) => setTimeout(res, ms));

function runToExit(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: 'inherit' });
    child.on('exit', (code, signal) => resolve(code !== null ? code : signal ? 1 : 0));
    child.on('error', reject);
  });
}

async function superviseDarwin(bundlePath, appDir) {
  const openCode = await runToExit('open', buildDarwinOpenArgs(bundlePath, appDir, process.env));
  if (openCode !== 0) {
    console.error(`[launch_electron] open exited ${openCode}`);
    process.exit(openCode);
  }
  // The app registers with the process table a beat after `open` returns.
  let appPid = null;
  for (let i = 0; i < 50 && appPid === null; i++) {
    appPid = currentAppPid(appDir);
    if (appPid === null) await sleep(100);
  }
  if (appPid === null) {
    console.error('[launch_electron] launched via open but could not locate the app process');
    process.exit(0);
  }
  // Forward termination so `concurrently -k` / Ctrl-C / `just minds-stop`
  // gracefully quit the (detached) app instead of orphaning it. A first signal
  // asks the app to quit and arms a force-kill fallback (the app's own shutdown
  // chain can stall); a second signal force-kills immediately. Either way the
  // launcher never hangs waiting on a wedged shutdown.
  const FORCE_KILL_GRACE_MS = 10000;
  let forwarding = false;
  const killAndExit = () => {
    try {
      process.kill(appPid, 'SIGKILL');
    } catch {
      /* already gone */
    }
    process.exit(0);
  };
  const forward = () => {
    if (forwarding) {
      killAndExit();
      return;
    }
    forwarding = true;
    try {
      process.kill(appPid, 'SIGTERM');
    } catch {
      /* already gone */
    }
    setTimeout(killAndExit, FORCE_KILL_GRACE_MS).unref();
  };
  process.on('SIGTERM', forward);
  process.on('SIGINT', forward);
  // Stay alive until the app exits.
  for (;;) {
    try {
      process.kill(appPid, 0);
    } catch {
      process.exit(0);
    }
    await sleep(500);
  }
}

async function main() {
  const appDir = path.resolve(process.argv[2] || '.');
  const bundlePath = process.platform === 'darwin' ? macAppBundleForBinary(electronBinary) : null;

  if (bundlePath && fs.existsSync(bundlePath)) {
    await superviseDarwin(bundlePath, appDir);
  } else {
    // Non-macOS, or an unexpected binary layout: exec the binary directly.
    process.exit(await runToExit(electronBinary, [appDir]));
  }
}

if (require.main === module) {
  main().catch((err) => {
    console.error('[launch_electron] failed to launch:', err.message);
    process.exit(1);
  });
}

module.exports = { macAppBundleForBinary, buildDarwinOpenArgs, findAppPidInPsLines };
