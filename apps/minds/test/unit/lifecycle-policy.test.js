// Unit tests for the window / quit lifecycle decisions.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). The decisions are
// pure helpers deliberately split out of main.js (which can't be required
// outside Electron) so they are testable here. The macOS keep-running-with-
// no-windows behavior can't be isolated by the e2e Playwright suite (it drives
// a real signed bundle and can't emit synthetic lifecycle events reliably), so
// this is the only place the platform-gated logic is verified.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  shouldQuitOnWindowAllClosed,
  shouldInterceptLastWindowClose,
  shouldOpenWindowOnActivate,
} = require('../../electron/lifecycle-policy');

test('window-all-closed: macOS never quits (app stays running with no windows)', () => {
  assert.equal(
    shouldQuitOnWindowAllClosed({ isMac: true, isShuttingDown: false, isQuitSequenceRunning: false }),
    false,
  );
});

test('window-all-closed: non-macOS quits when the last window closes', () => {
  assert.equal(
    shouldQuitOnWindowAllClosed({ isMac: false, isShuttingDown: false, isQuitSequenceRunning: false }),
    true,
  );
});

test('window-all-closed: an in-flight quit / shutdown short-circuits on every platform', () => {
  for (const isMac of [true, false]) {
    assert.equal(
      shouldQuitOnWindowAllClosed({ isMac, isShuttingDown: true, isQuitSequenceRunning: false }),
      false,
    );
    assert.equal(
      shouldQuitOnWindowAllClosed({ isMac, isShuttingDown: false, isQuitSequenceRunning: true }),
      false,
    );
  }
});

test('close interception: macOS never intercepts, so the last close leaves the app running', () => {
  assert.equal(
    shouldInterceptLastWindowClose({
      isMac: true,
      isShuttingDown: false,
      isQuitSequenceRunning: false,
      hasBackend: true,
      isLastLiveWindow: true,
    }),
    false,
  );
});

test('close interception: non-macOS intercepts the last live window to run the quit sequence', () => {
  assert.equal(
    shouldInterceptLastWindowClose({
      isMac: false,
      isShuttingDown: false,
      isQuitSequenceRunning: false,
      hasBackend: true,
      isLastLiveWindow: true,
    }),
    true,
  );
});

test('close interception: non-macOS does NOT intercept a non-last window', () => {
  assert.equal(
    shouldInterceptLastWindowClose({
      isMac: false,
      isShuttingDown: false,
      isQuitSequenceRunning: false,
      hasBackend: true,
      isLastLiveWindow: false,
    }),
    false,
  );
});

test('close interception: non-macOS does NOT intercept when there is no backend to tear down', () => {
  assert.equal(
    shouldInterceptLastWindowClose({
      isMac: false,
      isShuttingDown: false,
      isQuitSequenceRunning: false,
      hasBackend: false,
      isLastLiveWindow: true,
    }),
    false,
  );
});

test('close interception: an in-flight quit / shutdown short-circuits (non-macOS)', () => {
  assert.equal(
    shouldInterceptLastWindowClose({
      isMac: false,
      isShuttingDown: true,
      isQuitSequenceRunning: false,
      hasBackend: true,
      isLastLiveWindow: true,
    }),
    false,
  );
  assert.equal(
    shouldInterceptLastWindowClose({
      isMac: false,
      isShuttingDown: false,
      isQuitSequenceRunning: true,
      hasBackend: true,
      isLastLiveWindow: true,
    }),
    false,
  );
});

test('activate: opens a window only when none remain and no quit is in flight', () => {
  assert.equal(
    shouldOpenWindowOnActivate({ isShuttingDown: false, isQuitSequenceRunning: false, hasLiveWindow: false }),
    true,
  );
});

test('activate: with a live window already open the OS brings it forward -> no new window', () => {
  assert.equal(
    shouldOpenWindowOnActivate({ isShuttingDown: false, isQuitSequenceRunning: false, hasLiveWindow: true }),
    false,
  );
});

test('activate: never opens a window while a quit / shutdown is in flight', () => {
  assert.equal(
    shouldOpenWindowOnActivate({ isShuttingDown: true, isQuitSequenceRunning: false, hasLiveWindow: false }),
    false,
  );
  assert.equal(
    shouldOpenWindowOnActivate({ isShuttingDown: false, isQuitSequenceRunning: true, hasLiveWindow: false }),
    false,
  );
});
