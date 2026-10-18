'use strict';

// Pure decision logic for the desktop client's window / quit lifecycle.
// Kept free of any `electron` imports so it can be unit-tested under plain
// node (see ../test/unit/lifecycle-policy.test.js). main.js supplies the
// runtime flags (platform, in-flight quit state, live window counts, the
// launch argv) and acts on what each helper returns.
//
// The convention these encode, on every platform: closing every window leaves
// the app running behind its tray icon; the user re-opens a window from the
// tray, the dock, or by launching the app again (see the 'activate' and
// 'second-instance' handlers) and quits explicitly, from the tray's Quit or
// Cmd+Q / Ctrl+Q. An agent that needs the app's API starts it with
// `--background`, which runs the backend and the tray and opens no window.

// The switch an agent starts the app with to get the backend and the tray and
// no window (see specs/imbue-studio-agent-access/spec.md).
const BACKGROUND_SWITCH = '--background';

/**
 * Whether an argv (a cold start's `process.argv`, or the one `second-instance`
 * forwards) asks for a background start.
 *
 * @param {string[]} argv
 * @returns {boolean}
 */
function isBackgroundLaunch(argv) {
  if (!Array.isArray(argv)) return false;
  return argv.includes(BACKGROUND_SWITCH);
}

/**
 * What a second launch of the app (delivered to the running instance by the
 * single-instance lock) should do with its argv.
 *
 *   'deeplink'  the argv carries a deeplink URL: handle it.
 *   'ignore'    the argv asks for a background start: an agent making sure the
 *               app is up, which must not pop a window over the user's work.
 *   'reopen'    a plain second launch: bring the app forward, reopening a
 *               window when none is open.
 *
 * The deeplink wins over the background switch because a URL arriving is an
 * explicit ask to show something, and nothing launches with both.
 *
 * @param {object} state
 * @param {boolean} state.hasDeeplinkUrl     The argv carries an imbue-studio:// URL.
 * @param {boolean} state.isBackgroundLaunch The argv carries the background switch.
 * @returns {'deeplink'|'ignore'|'reopen'}
 */
function decideSecondInstanceAction({ hasDeeplinkUrl, isBackgroundLaunch: isBackground }) {
  if (hasDeeplinkUrl) return 'deeplink';
  if (isBackground) return 'ignore';
  return 'reopen';
}

/**
 * Whether the macOS dock icon should be showing: only while a window is open.
 * The tray icon is the app's one persistent presence, so the dock reflects
 * open windows the way it does for menu-bar apps that also have windows.
 *
 * @param {object} state
 * @param {boolean} state.isMac          Running on macOS (darwin).
 * @param {boolean} state.hasLiveWindow  At least one non-destroyed window exists.
 * @returns {boolean}
 */
function shouldShowDockIcon({ isMac, hasLiveWindow }) {
  if (!isMac) return false;
  return hasLiveWindow;
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
 *                     a pending startup route belongs to the window that is
 *                     already waiting for it, and reopening an app that has a
 *                     window means showing that window.
 *   'error-takeover'  open shell.html replaying the recorded error, whose
 *                     Retry button restarts the backend.
 *   'loading'         open shell.html's loading screen; the backend is still
 *                     coming up.
 *   'startup-route'   open a window and land the app's FIRST-window route on
 *                     it (session restore / start flow / consent). Reached when
 *                     the launch never got to land anywhere -- its window was
 *                     closed mid-startup -- and also during the window between
 *                     the backend publishing its URL and that route being
 *                     computed, so a request in that window waits on the
 *                     loading screen instead of landing on home and being
 *                     yanked off it a moment later. Also reached when the app
 *                     is reopened with no window open (a dock-icon click or a
 *                     second launch): reopening lands where a fresh launch
 *                     would, back on the workspaces open when the last window
 *                     closed.
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
 * @param {boolean} state.isReopen                The request reopens the app (dock-icon click,
 *                                                second launch) rather than asking for a new
 *                                                window (Cmd+N, File > New Window, the dock menu).
 * @returns {'none'|'focus-existing'|'error-takeover'|'loading'|'startup-route'|'home'}
 */
function decideNewWindowTarget({
  hasBackendUrl,
  hasErrorTakeover,
  hasLiveWindow,
  isStartupRoutingPending,
  isShuttingDown,
  isQuitSequenceRunning,
  isReopen,
}) {
  if (isShuttingDown || isQuitSequenceRunning) return 'none';
  if (hasErrorTakeover) return hasLiveWindow ? 'focus-existing' : 'error-takeover';
  if (!hasBackendUrl) return hasLiveWindow ? 'focus-existing' : 'loading';
  if (isStartupRoutingPending || isReopen) return hasLiveWindow ? 'focus-existing' : 'startup-route';
  return 'home';
}

module.exports = {
  BACKGROUND_SWITCH,
  shouldOpenWindowOnActivate,
  decideNewWindowTarget,
  isBackgroundLaunch,
  decideSecondInstanceAction,
  shouldShowDockIcon,
};
