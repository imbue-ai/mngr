// Unit tests for what the startup error takeover says, which log it opens, and
// what its Retry runs again.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  RETRY_START,
  crashTakeover,
  logFileNameFor,
  retryStartsFrom,
  setupFailureTakeover,
  startFailureTakeover,
} = require('../../electron/startup-failure');
const {
  SelfContainedStartupError,
  describeLaunch,
  formatEarlyExitDetails,
  formatSpawnFailureDetails,
} = require('../../electron/backend-exit-diagnostics');

// The shapes env-setup.js rejects with: its own prefix, then uv's output.
const UV_OFFLINE_OUTPUT =
  'uv sync failed with exit code 2\n\n' +
  'Resolved 212 packages in 4ms\n' +
  'error: Failed to download `pydantic-core==2.33.2`\n' +
  '  Caused by: Request failed after 3 retries\n' +
  '  Caused by: error sending request for url (https://files.pythonhosted.org/packages/...)\n' +
  '  Caused by: dns error: failed to lookup address information: nodename nor servname provided, or not known';
const UV_DISK_FULL_OUTPUT =
  'uv sync failed with exit code 2\n\n' +
  'error: Failed to download `pydantic-core==2.33.2`\n' +
  '  Caused by: failed to write to file `/Users/x/Library/Caches/uv/...`: No space left on device (os error 28)';

test('a setup that could not download asks for the internet connection, with uv output behind the details', () => {
  const takeover = setupFailureTakeover(new Error(UV_OFFLINE_OUTPUT));
  assert.equal(
    takeover.message,
    "Imbue Studio couldn't finish setting itself up. *Check your internet connection, then press Retry.*",
  );
  assert.equal(takeover.details, UV_OFFLINE_OUTPUT);
});

test('a full disk is named as such, even when the write that failed was a download', () => {
  const takeover = setupFailureTakeover(new Error(UV_DISK_FULL_OUTPUT));
  assert.equal(
    takeover.message,
    "Imbue Studio couldn't finish setting itself up. Your disk is full. *Free up some space, then press Retry.*",
  );
});

test('a log folder that could not be created for lack of space is a full disk too', () => {
  const error = Object.assign(new Error("ENOSPC: no space left on device, mkdir '/Users/x/Library/Logs/Imbue Studio'"), {
    code: 'ENOSPC',
  });
  assert.match(setupFailureTakeover(error).message, /Your disk is full/);
});

test('any other setup failure makes no claim about the network', () => {
  for (const output of [
    'Failed to start uv: spawn /Applications/Imbue Studio.app/Contents/Resources/uv/uv EACCES\n\n',
    "EACCES: permission denied, mkdir '/Users/x/Library/Logs/Imbue Studio'",
    'uv sync failed with exit code 1\n\nerror: Failed to create virtual environment\n  Caused by: Permission denied (os error 13)',
  ]) {
    const takeover = setupFailureTakeover(new Error(output));
    assert.equal(
      takeover.message,
      "Imbue Studio couldn't finish setting itself up. Try again. If this persists, report a bug.",
    );
    assert.equal(takeover.details, output);
  }
});

// minds.log's tail as main.js reads it: this launch's backend output, timestamped.
const RECENT_LOG =
  '[2026-10-30T17:02:11.204Z] INFO  Starting the forward on port 50112\n' +
  '[2026-10-30T17:02:21.877Z] WARNING the forward did not come up: bind failed (address already in use)';
const LAUNCH = describeLaunch({ isDev: false, hasConfigFileArg: true, hasInheritedClientConfigEnv: false });

test('a backend that never answered on its port keeps how it ended and the log tail behind the details', () => {
  const summary =
    'Backend emitted login URL but server never became ready: Server not ready after 50 attempts on port 50112';
  const takeover = startFailureTakeover(new Error(summary), RECENT_LOG);
  assert.equal(takeover.message, "Imbue Studio couldn't start. Try again. If this persists, report a bug.");
  assert.equal(takeover.details, `${summary}\n\n${RECENT_LOG}`);
});

test('the log tail behind the details never carries the unconsumed one-time login code', () => {
  const recentLog =
    '[2026-10-30T17:02:10.913Z] INFO  Imbue Studio login URL (one-time use): http://localhost:50112/login?one_time_code=s3cr3t\n' +
    '[2026-10-30T17:02:10.914Z] {"event": "login_url", "login_url": "http://localhost:50112/login?one_time_code=s3cr3t"}\n' +
    RECENT_LOG;
  const summary = 'Backend emitted login URL but server never became ready';
  assert.equal(startFailureTakeover(new Error(summary), recentLog).details, `${summary}\n\n${RECENT_LOG}`);
  assert.equal(crashTakeover(1, recentLog).details, RECENT_LOG);
});

test('a backend that died before its login URL shows its own last stderr once, without the log tail repeating it', () => {
  const stderrLines = ['Traceback (most recent call last):', '  ...', 'DeviceIdError: could not read the device id'];
  const summary = formatEarlyExitDetails({
    code: 1,
    signal: null,
    elapsedMs: 2140,
    stdoutEventCount: 0,
    launch: LAUNCH,
    stderrLines,
  });
  const recentLog = stderrLines.map((line) => `[2026-10-30T17:02:11.204Z] ${line}`).join('\n');
  const takeover = startFailureTakeover(new SelfContainedStartupError(summary), recentLog);
  assert.equal(takeover.details, summary);
});

test('a backend that never ran shows no earlier launch\'s log as if it explained this one', () => {
  const spawnFailure = new SelfContainedStartupError(
    formatSpawnFailureDetails({ errorMessage: 'spawn uv ENOENT', launch: LAUNCH }),
  );
  assert.equal(startFailureTakeover(spawnFailure, RECENT_LOG).details, spawnFailure.message);
});

test('a failed start with no backend log still says how it ended', () => {
  const takeover = startFailureTakeover(new Error('Backend emitted login URL but server never became ready'), '');
  assert.equal(takeover.details, 'Backend emitted login URL but server never became ready');
});

test('Retry after a setup failure runs setup again; every other retry restarts only the backend', () => {
  assert.equal(retryStartsFrom(setupFailureTakeover(new Error(UV_OFFLINE_OUTPUT))), RETRY_START.ENV_SETUP);
  assert.equal(retryStartsFrom(startFailureTakeover(new Error('Backend exited with code 1'), '')), RETRY_START.BACKEND);
  assert.equal(retryStartsFrom(crashTakeover(1, 'last lines')), RETRY_START.BACKEND);
  // "Restart Imbue Studio" from a running app: there is no takeover at all.
  assert.equal(retryStartsFrom(null), RETRY_START.BACKEND);
});

test('View full log opens the log that holds the failure on screen', () => {
  // Setup runs before the backend exists, so its output is only in electron.log.
  assert.equal(logFileNameFor(setupFailureTakeover(new Error(UV_OFFLINE_OUTPUT))), 'electron.log');
  assert.equal(logFileNameFor(startFailureTakeover(new Error('Backend exited with code 1'), '')), 'minds.log');
  assert.equal(logFileNameFor(crashTakeover(1, '')), 'minds.log');
  assert.equal(logFileNameFor(null), 'minds.log');
});
