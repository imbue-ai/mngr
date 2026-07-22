'use strict';

// Pure helpers for persisting the desktop client's window-state.json.
//
// Kept free of any `electron` imports (like startup-routing.js and
// view-layout.js) so both pieces can be unit-tested under plain node -- see
// ../test/unit/session-persistence.test.js. main.js wires these to the real
// window set: it schedules a debounced save on window move/resize/navigation
// and applies the empty-clobber guard inside saveSessionState.

/**
 * Decide whether a freshly-computed session-state window list may be written
 * over the current on-disk window-state.json.
 *
 * Why this guard exists: the window list is captured from the LIVE window set
 * at save time. A non-graceful quit -- the ToDesktop "Install and Restart"
 * auto-update path, a crash, or a force-quit -- tears the windows down
 * concurrently with the save, so the captured list can come back EMPTY even
 * though the user had windows open. Writing that empty list clobbers a
 * perfectly good window-state.json, and the next launch computes
 * ``restorableCount === 0`` and drops the user on the create screen instead of
 * their restored workspace.
 *
 * Rule: an empty computed list may only be written when the on-disk file is
 * ALSO empty (or missing / unreadable -- ``persistedWindowCount === 0``). If
 * the computed list is empty but a non-empty set is already persisted, treat it
 * as a teardown-race artifact and skip the write, preserving the good file.
 *
 * This never rejects a legitimate save: while the app is running with windows
 * open, every save computes a non-empty list. The only in-app path to zero
 * windows is closing the last window, and that saves through the quit sequence
 * while the last window is still alive (a non-empty list). So a computed-empty
 * list while a non-empty file exists is always a race, never a real state.
 *
 * @param {object} counts
 * @param {number} counts.computedWindowCount   Windows in the just-computed snapshot.
 * @param {number} counts.persistedWindowCount  Windows in the current on-disk file.
 * @returns {boolean} true if the snapshot should be written to disk.
 */
function shouldWriteSessionState({ computedWindowCount, persistedWindowCount }) {
  if (computedWindowCount > 0) return true;
  return persistedWindowCount === 0;
}

/**
 * Create a trailing-throttle scheduler that coalesces a burst of ``schedule()``
 * calls into at most one ``save()`` per ``delayMs``.
 *
 * Semantics: the first ``schedule()`` after an idle period arms a timer;
 * further ``schedule()`` calls while that timer is pending are coalesced into
 * it (no new timer). When the timer fires, ``save()`` runs once and the
 * scheduler returns to idle. This bounds disk writes to at most one per
 * interval even under continuous activity (a window drag/resize fires move/
 * resize events every frame), while still flushing promptly after the burst.
 *
 * ``setTimer`` / ``clearTimer`` are injectable so tests can drive it without
 * real timers; they default to the global timer functions.
 *
 * @param {object} options
 * @param {() => void} options.save          Persist callback (runs the actual write).
 * @param {number} options.delayMs           Coalescing window in milliseconds.
 * @param {Function} [options.setTimer]      Defaults to global setTimeout.
 * @param {Function} [options.clearTimer]    Defaults to global clearTimeout.
 * @returns {{ schedule: () => void, flush: () => void, cancel: () => void, isPending: () => boolean }}
 */
function createDebouncedSaver({ save, delayMs, setTimer, clearTimer }) {
  const arm = setTimer || setTimeout;
  const disarm = clearTimer || clearTimeout;
  let timerId = null;

  function schedule() {
    if (timerId !== null) return; // a flush is already pending -- coalesce into it
    timerId = arm(() => {
      timerId = null;
      save();
    }, delayMs);
  }

  // Cancel any pending timer without saving (e.g. once a quit takes over and
  // performs its own authoritative save).
  function cancel() {
    if (timerId !== null) {
      disarm(timerId);
      timerId = null;
    }
  }

  // Cancel any pending timer and save immediately (the coalesced work is due).
  function flush() {
    const wasPending = timerId !== null;
    cancel();
    if (wasPending) save();
  }

  function isPending() {
    return timerId !== null;
  }

  return { schedule, flush, cancel, isPending };
}

module.exports = { shouldWriteSessionState, createDebouncedSaver };
