'use strict';

// Log lines for the macOS `activate` handler and the per-window `focus` relay.
// Kept free of any `electron` imports so it can be unit-tested under plain node.
//
// A fatal EXC_BREAKPOINT (a Chromium CHECK) has killed the browser process
// about a second after a dock click raised a window, with JS frames on the
// crashing stack but every Electron frame unsymbolicated. The app JS that runs
// at that moment is the `activate` handler and the focus relay, so each logs a
// line before and after the call that opens or raises a window
// (`openOrFocusWindow`) or relays the focus to the page. Main-process console
// output becomes breadcrumbs on the minidump event uploaded at the next launch:
// a "start" line with no matching "end" line pins the crash inside that call,
// and two complete pairs rule both calls out. The window-state reads that
// build a "start" line run before it, so they are not covered.
//
// Only booleans and Electron's numeric window id are logged, never a URL or
// any window content.

/**
 * The line logged on entry to the `activate` handler.
 *
 * @param {object} state
 * @param {boolean} state.hasLiveWindow         A non-destroyed window (popouts included) existed.
 * @param {boolean} state.isShuttingDown        A shutdown had already committed.
 * @param {boolean} state.isQuitSequenceRunning The quit sequence was already running.
 * @param {boolean} state.opensWindow           `shouldOpenWindowOnActivate` returned true, so
 *                                              `openOrFocusWindow` is about to be called.
 * @returns {string}
 */
function formatActivateStart({ hasLiveWindow, isShuttingDown, isQuitSequenceRunning, opensWindow }) {
  return (
    `[activate] start (hasLiveWindow=${hasLiveWindow}, isShuttingDown=${isShuttingDown}, ` +
    `isQuitSequenceRunning=${isQuitSequenceRunning}, opensWindow=${opensWindow})`
  );
}

/**
 * The line logged once the `activate` handler has returned from every Electron call.
 *
 * @param {object} state
 * @param {boolean} state.openedWindow `openOrFocusWindow` was called and returned.
 * @returns {string}
 */
function formatActivateEnd({ openedWindow }) {
  return `[activate] end (openedWindow=${openedWindow})`;
}

/**
 * The line logged in a window's `focus` handler before the focus is relayed to its page.
 *
 * @param {object} state
 * @param {number} state.windowId            The BrowserWindow's numeric id.
 * @param {boolean} state.isWindowDestroyed  `win.isDestroyed()`.
 * @param {boolean} state.isContentsDestroyed `win.webContents.isDestroyed()`, or true with the window.
 * @param {boolean} state.isPopout           The window is a popout.
 * @returns {string}
 */
function formatFocusRelayStart({ windowId, isWindowDestroyed, isContentsDestroyed, isPopout }) {
  return (
    `[focus] window ${windowId} focused, relaying (windowDestroyed=${isWindowDestroyed}, ` +
    `contentsDestroyed=${isContentsDestroyed}, popout=${isPopout})`
  );
}

/**
 * The line logged once a window's focus has been relayed to its page.
 *
 * @param {object} state
 * @param {number} state.windowId The BrowserWindow's numeric id.
 * @returns {string}
 */
function formatFocusRelayEnd({ windowId }) {
  return `[focus] window ${windowId} focus relayed`;
}

module.exports = { formatActivateStart, formatActivateEnd, formatFocusRelayStart, formatFocusRelayEnd };
