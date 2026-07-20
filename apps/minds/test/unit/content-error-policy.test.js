// Unit tests for the content-load error-page gate.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// The filtering is the bug-prone part of the did-fail-load handler (a wrong
// filter either blanks the view on every benign superseded load, or never shows
// the error page at all), so it is split into the pure ``shouldShowContentLoadError``
// helper -- deliberately out of main.js, which can't be required outside Electron.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { shouldShowContentLoadError, ERR_ABORTED } = require('../../electron/content-error-policy');

// A genuine network-layer failure of the top-level workspace load.
const REAL_FAILURE = {
  errorCode: -102, // ERR_CONNECTION_REFUSED
  isMainFrame: true,
  isLocalShellPage: false,
  isShuttingDown: false,
  isErrorState: false,
};

test('a real main-frame connection failure shows the error page', () => {
  assert.equal(shouldShowContentLoadError(REAL_FAILURE), true);
});

test('other network failures (DNS, TLS, timeout) also show the error page', () => {
  for (const errorCode of [-105 /* NAME_NOT_RESOLVED */, -201 /* CERT_DATE_INVALID */, -7 /* TIMED_OUT */]) {
    assert.equal(shouldShowContentLoadError({ ...REAL_FAILURE, errorCode }), true, `code ${errorCode}`);
  }
});

test('ERR_ABORTED (a superseded load) never shows the error page', () => {
  assert.equal(shouldShowContentLoadError({ ...REAL_FAILURE, errorCode: ERR_ABORTED }), false);
  assert.equal(shouldShowContentLoadError({ ...REAL_FAILURE, errorCode: -3 }), false);
});

test('a subframe (iframe) failure never blanks the whole view', () => {
  assert.equal(shouldShowContentLoadError({ ...REAL_FAILURE, isMainFrame: false }), false);
});

test('a failure loading one of our own local shell pages does not recurse', () => {
  assert.equal(shouldShowContentLoadError({ ...REAL_FAILURE, isLocalShellPage: true }), false);
});

test('no error page during shutdown', () => {
  assert.equal(shouldShowContentLoadError({ ...REAL_FAILURE, isShuttingDown: true }), false);
});

test('no error page while the full-app error takeover owns the screen', () => {
  assert.equal(shouldShowContentLoadError({ ...REAL_FAILURE, isErrorState: true }), false);
});

test('missing/empty input is treated as "do not show" rather than throwing', () => {
  assert.equal(shouldShowContentLoadError(), false);
  assert.equal(shouldShowContentLoadError({}), false);
});
