'use strict';

// Pure decision logic for the desktop client's window / quit lifecycle.
// Kept free of any `electron` imports so it can be unit-tested under plain
// node (see ../test/unit/lifecycle-policy.test.js). main.js supplies the
// runtime flags (platform, in-flight quit state, backend presence, live
// window counts) and acts on the boolean each helper returns.
//
// The macOS convention these encode: closing every window leaves the app
// running (the dock icon stays); the user re-opens a window from the dock
// (see the 'activate' handler) and quits explicitly with Cmd+Q. Windows and
// Linux keep the historical behavior of quitting when the last window closes.

/**
 * Whether the app should begin quitting when the last window closes (the
 * `window-all-closed` event fires).
 *
 * On macOS the app stays alive with no windows, so this is always false.
 * Elsewhere the app quits, unless a quit / shutdown is already in flight.
 *
 * @param {object} state
 * @param {boolean} state.isMac                Running on macOS (darwin).
 * @param {boolean} state.isShuttingDown       A shutdown has already committed.
 * @param {boolean} state.isQuitSequenceRunning The quit sequence is already running.
 * @returns {boolean}
 */
function shouldQuitOnWindowAllClosed({ isMac, isShuttingDown, isQuitSequenceRunning }) {
  if (isShuttingDown || isQuitSequenceRunning) return false;
  return !isMac;
}

/**
 * Whether a window's `close` event should be intercepted to run the quit
 * sequence *before* the window disappears (so the local-mind shutdown prompt
 * can appear while a window is still visible).
 *
 * Only the last live window triggers a quit, and only off macOS: on macOS
 * closing the last window is an ordinary close that leaves the app running,
 * so the prompt fires only on an explicit Quit instead.
 *
 * @param {object} state
 * @param {boolean} state.isMac                 Running on macOS (darwin).
 * @param {boolean} state.isShuttingDown        A shutdown has already committed.
 * @param {boolean} state.isQuitSequenceRunning The quit sequence is already running.
 * @param {boolean} state.hasBackend            The backend process is alive.
 * @param {boolean} state.isLastLiveWindow      This is the only non-destroyed window.
 * @returns {boolean}
 */
function shouldInterceptLastWindowClose({
  isMac,
  isShuttingDown,
  isQuitSequenceRunning,
  hasBackend,
  isLastLiveWindow,
}) {
  if (isMac) return false;
  return !isShuttingDown && !isQuitSequenceRunning && hasBackend && isLastLiveWindow;
}

/**
 * Whether activating the app (e.g. a macOS dock-icon click) should open a
 * fresh window. Only when the app isn't quitting and no live window remains --
 * with a window already open the OS just brings it forward.
 *
 * @param {object} state
 * @param {boolean} state.isShuttingDown        A shutdown has already committed.
 * @param {boolean} state.isQuitSequenceRunning The quit sequence is already running.
 * @param {boolean} state.hasLiveWindow         At least one non-destroyed window exists.
 * @returns {boolean}
 */
function shouldOpenWindowOnActivate({ isShuttingDown, isQuitSequenceRunning, hasLiveWindow }) {
  if (isShuttingDown || isQuitSequenceRunning) return false;
  return !hasLiveWindow;
}

module.exports = {
  shouldQuitOnWindowAllClosed,
  shouldInterceptLastWindowClose,
  shouldOpenWindowOnActivate,
};
