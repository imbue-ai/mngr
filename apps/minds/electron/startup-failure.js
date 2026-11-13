'use strict';

// What the startup error takeover (shell.html's "Something went wrong") says
// for each way a launch can fail, which log holds that failure, and what its
// Retry has to run again. Free of any `electron` import so it can be
// unit-tested under plain node.

const { SelfContainedStartupError } = require('./backend-exit-diagnostics');
const { PRODUCT_DISPLAY_NAME } = require('./product-name');
const { isSecretStartupLogLine } = require('./startup-log');

const TRY_AGAIN_SENTENCE = 'Try again. If this persists, report a bug.';

// The setup step is `uv sync`. These are the texts uv prints for the two
// causes a person can fix themselves; a full disk is checked first because a
// download that could not be written to disk also reads "Failed to download".
// ENOSPC is Node's own code, for the log folder that could not be created.
const DISK_FULL_PATTERN = /no space left on device|ENOSPC/i;
const DOWNLOAD_FAILED_PATTERN =
  /Failed to download|Failed to fetch|Request failed after|error sending request|failed to lookup address information|dns error/i;

const TAKEOVER_KIND = Object.freeze({
  SETUP: 'setup',
  START: 'start',
  CRASH: 'crash',
});

const RETRY_START = Object.freeze({
  ENV_SETUP: 'env-setup',
  BACKEND: 'backend',
});

function setupFailureSentence(output) {
  if (DISK_FULL_PATTERN.test(output)) return 'Your disk is full. *Free up some space, then press Retry.*';
  if (DOWNLOAD_FAILED_PATTERN.test(output)) return '*Check your internet connection, then press Retry.*';
  return TRY_AGAIN_SENTENCE;
}

// minds.log keeps the backend's login URL, one-time code and all, and a start
// that never answered left that code unconsumed, so the takeover's copy of the
// log drops what the loading screen's log drops.
function withoutSecretLines(logLines) {
  return (logLines || '')
    .split('\n')
    .filter((line) => !isSecretStartupLogLine(line))
    .join('\n');
}

/** The takeover for a failed environment setup (`runEnvSetup` rejected). */
function setupFailureTakeover(error) {
  const output = error && error.message ? error.message : String(error);
  return {
    kind: TAKEOVER_KIND.SETUP,
    message: `${PRODUCT_DISPLAY_NAME} couldn't finish setting itself up. ${setupFailureSentence(output)}`,
    details: output,
  };
}

/**
 * The takeover for a backend that could not be started. An error that carries
 * the backend's own output (or that came before there was any) is the whole
 * story. Any other -- a backend that printed its login URL but never answered
 * on its port, a failure while routing the first window -- says only how the
 * start ended, and the reason is in what the backend printed, so the log's last
 * lines go with it.
 */
function startFailureTakeover(error, recentLogLines) {
  const summary = error && error.message ? error.message : String(error);
  const logTail = withoutSecretLines(recentLogLines);
  const isLogTailAppended = Boolean(logTail) && !(error instanceof SelfContainedStartupError);
  return {
    kind: TAKEOVER_KIND.START,
    message: `${PRODUCT_DISPLAY_NAME} couldn't start. ${TRY_AGAIN_SENTENCE}`,
    details: isLogTailAppended ? `${summary}\n\n${logTail}` : summary,
  };
}

/** The takeover for a backend that died after it had started. */
function crashTakeover(exitCode, recentLogLines) {
  return {
    kind: TAKEOVER_KIND.CRASH,
    message: `${PRODUCT_DISPLAY_NAME} stopped unexpectedly`,
    details: withoutSecretLines(recentLogLines) || `Process exited with code ${exitCode}`,
  };
}

/**
 * Where a Retry has to start over from. A setup failure never got as far as
 * the backend, so retrying only the backend would skip the step that failed.
 * Everything else restarts the backend alone, including "Restart Imbue
 * Studio" from a live app, which has no takeover.
 */
function retryStartsFrom(takeover) {
  return takeover && takeover.kind === TAKEOVER_KIND.SETUP ? RETRY_START.ENV_SETUP : RETRY_START.BACKEND;
}

/**
 * The log "View full log" opens: the one that holds the failure on screen.
 * Setup runs before the backend exists, so its output is only in the main
 * process's own log; every later failure is the backend's.
 */
function logFileNameFor(takeover) {
  return takeover && takeover.kind === TAKEOVER_KIND.SETUP ? 'electron.log' : 'minds.log';
}

module.exports = {
  RETRY_START,
  crashTakeover,
  logFileNameFor,
  retryStartsFrom,
  setupFailureTakeover,
  startFailureTakeover,
};
