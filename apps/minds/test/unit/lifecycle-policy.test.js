// Unit tests for the window / quit lifecycle decisions.
//
// Run with: pnpm --dir apps/minds test:unit   (or: node --test test/unit/)
//
// These use node's built-in test runner (zero extra deps). The decisions are
// pure helpers deliberately split out of main.js (which can't be required
// outside Electron) so the platform-gated branching is testable here. The
// end-to-end behavior these drive -- closing every window, then reopening from
// the dock in each app state -- is covered by test/e2e/macos-lifecycle.spec.js,
// which emits the lifecycle events into a real bundle's main process.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const {
  shouldQuitOnWindowAllClosed,
  shouldInterceptLastWindowClose,
  shouldOpenWindowOnActivate,
  decideNewWindowTarget,
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

// -- decideNewWindowTarget --
//
// The invariant these guard: with the app alive and no windows open, every
// "give me a window" request must resolve to a window. Returning nothing was
// the #480 / #482 / #483 defect -- an app inert in the dock until Cmd+Q.
//
// READY is the steady state (backend serving, first-window route landed); each
// test overrides only the fields it is about, so a new field added to the
// decision cannot silently change what these assert.
const READY = {
  hasBackendUrl: true,
  hasErrorTakeover: false,
  hasLiveWindow: false,
  isStartupRoutingPending: false,
  isShuttingDown: false,
  isQuitSequenceRunning: false,
};
const decide = (overrides) => decideNewWindowTarget({ ...READY, ...overrides });

test('new window: a serving backend opens the home page', () => {
  assert.equal(decide({}), 'home');
});

test('new window: with no window and a failed startup, the error screen (and its Retry) opens', () => {
  assert.equal(decide({ hasBackendUrl: false, hasErrorTakeover: true }), 'error-takeover');
});

test('new window: with no window and a backend still starting, the loading screen opens', () => {
  assert.equal(decide({ hasBackendUrl: false }), 'loading');
});

test('new window: a dead backend is never reopened at its stale URL', () => {
  // The backend published a URL and then crashed. Loading that port again is
  // what left the user on "This screen failed to load" with a Reload button
  // that re-loaded the same dead port.
  assert.equal(decide({ hasErrorTakeover: true }), 'error-takeover');
});

test('new window: a live window is focused rather than duplicating a takeover screen', () => {
  assert.equal(decide({ hasBackendUrl: false, hasLiveWindow: true }), 'focus-existing');
  assert.equal(decide({ hasErrorTakeover: true, hasLiveWindow: true }), 'focus-existing');
  assert.equal(decide({ isStartupRoutingPending: true, hasLiveWindow: true }), 'focus-existing');
});

test('new window: a serving backend still opens a SECOND window (Cmd+N is not a focus)', () => {
  assert.equal(decide({ hasLiveWindow: true }), 'home');
});

// The route the launch computed is owed to the first window. Reached two ways:
// the startup window was closed mid-startup (#481), or the request arrived in
// the gap between the backend publishing its URL and the route being computed.
test('new window: an unlanded first-window route is claimed instead of landing on home', () => {
  assert.equal(decide({ isStartupRoutingPending: true }), 'startup-route');
});

test('new window: the startup route outranks home but not a failure', () => {
  // Backend serving and a route owed -> route wins over a plain home load...
  assert.equal(decide({ isStartupRoutingPending: true }), 'startup-route');
  // ...but a crash mid-startup still shows the error screen, not a restore.
  assert.equal(decide({ isStartupRoutingPending: true, hasErrorTakeover: true }), 'error-takeover');
  assert.equal(decide({ isStartupRoutingPending: true, hasBackendUrl: false }), 'loading');
});

test('new window: a committed quit opens nothing, in every state', () => {
  // Matches the three sibling decisions in this module, which all short-circuit
  // on an in-flight quit. Without this, second-instance and the deeplink path
  // could construct a window after runQuitSequence committed.
  for (const flag of ['isShuttingDown', 'isQuitSequenceRunning']) {
    assert.equal(decide({ [flag]: true }), 'none');
    assert.equal(decide({ [flag]: true, hasLiveWindow: true }), 'none');
    assert.equal(decide({ [flag]: true, hasErrorTakeover: true }), 'none');
    assert.equal(decide({ [flag]: true, isStartupRoutingPending: true }), 'none');
  }
});
