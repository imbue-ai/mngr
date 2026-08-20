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

/**
 * What a "give me a window" request should produce: a dock-icon activate,
 * Cmd+N / File > New Window, the dock menu, a second launch, or a deeplink
 * arriving with nothing open.
 *
 * Because the app now sits alive with zero windows, such a request can arrive
 * in any app state, and it must always land on a window reflecting the real
 * one. A request that produces nothing leaves an app that can never open a
 * window again, recoverable only with Cmd+Q.
 *
 * The outcomes:
 *   'none'            a quit has committed -- opening a window would fight it.
 *   'focus-existing'  bring the most recent window forward. A second loading
 *                     or error window would say nothing the open one doesn't,
 *                     and a pending startup route belongs to the window that
 *                     is already waiting for it.
 *   'error-takeover'  open shell.html replaying the recorded error, whose
 *                     Retry button restarts the backend.
 *   'loading'         open shell.html's loading screen; the backend is still
 *                     coming up.
 *   'startup-route'   open a window and land the app's FIRST-window route on
 *                     it (session restore / welcome / consent). Reached when
 *                     the launch never got to land anywhere -- its window was
 *                     closed mid-startup -- and also during the window between
 *                     the backend publishing its URL and that route being
 *                     computed, so a request in that window waits on the
 *                     loading screen instead of landing on home and being
 *                     yanked off it a moment later.
 *   'home'            load the backend's home page.
 *
 * Error state is checked before the backend URL because after a crash the URL
 * is still set and still names the dead port: routing there is the failure the
 * error screen exists to replace.
 *
 * @param {object} state
 * @param {boolean} state.hasBackendUrl           The backend has published a base URL.
 * @param {boolean} state.hasErrorTakeover        A startup / crash error is current.
 * @param {boolean} state.hasLiveWindow           At least one non-destroyed window exists.
 * @param {boolean} state.isStartupRoutingPending The first-window route has not landed yet.
 * @param {boolean} state.isShuttingDown          A shutdown has already committed.
 * @param {boolean} state.isQuitSequenceRunning   The quit sequence is already running.
 * @returns {'none'|'focus-existing'|'error-takeover'|'loading'|'startup-route'|'home'}
 */
function decideNewWindowTarget({
  hasBackendUrl,
  hasErrorTakeover,
  hasLiveWindow,
  isStartupRoutingPending,
  isShuttingDown,
  isQuitSequenceRunning,
}) {
  if (isShuttingDown || isQuitSequenceRunning) return 'none';
  if (hasErrorTakeover) return hasLiveWindow ? 'focus-existing' : 'error-takeover';
  if (!hasBackendUrl) return hasLiveWindow ? 'focus-existing' : 'loading';
  if (isStartupRoutingPending) return hasLiveWindow ? 'focus-existing' : 'startup-route';
  return 'home';
}

module.exports = {
  shouldQuitOnWindowAllClosed,
  shouldInterceptLastWindowClose,
  shouldOpenWindowOnActivate,
  decideNewWindowTarget,
};
