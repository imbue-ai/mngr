# Plan: Loading-window position jump on startup

> When I start up the app the loading window appears, then when the app is ready it jumps to another spot. Make the loading screen window open at the same location as the previous session.
>
> * Reuse the existing `restoreWindowBounds()` helper to apply saved bounds to the initial window before its loading screen renders.
> * Persist saved window state in most-recently-focused (MRU) order so that for multi-window users entry 0 is the last window the user actually interacted with, not whichever window happened to be created first.
> * For multi-window restore, open the lesser-MRU windows without stealing focus so the MRU-zero window (already shown as the initial loading window) stays in front.
> * First-ever launch (no `window-state.json`) keeps the default centered 1200x800.

## Overview

- Bug: `createBundle()` constructs the initial `BaseWindow` with default 1200x800 and no x/y, so Electron centers it. The loading screen renders at that centered default. Once the backend is up, `restoreWindowBounds()` moves the window -- that's the visible jump.
- Core fix: between `createBundle()` and `runStartupSequence()` at the existing startup site (around `main.js:1491`), load the saved state and call `restoreWindowBounds()` on the new window with entry 0. The initial `BaseWindow` is created with `show: false` (`main.js:281`), so `setBounds` applied before `show()` is invisible to the user -- no flash at the default position.
- Related fix: persist session state in MRU order so entry 0 corresponds to the user's last-active window. Without this, the loading screen would appear at the position of whichever window happened to be first in the `bundles` Set, which is creation order, not recency.
- Related fix: restoring multiple windows opens N-1 BaseWindows on top of the initial loading window. By default each one would `show()` and steal focus from the others. Add an opt-in `showInactive` mode to `openNewWindow` so the lesser-MRU windows surface without focus, and the MRU-zero window (initialBundle) stays in front after the dust settles. After restore, rewrite `mruWindows` so the saved order is preserved across the restart (otherwise `createBundle`'s unshift reverses it).

## Expected behavior

- First-ever launch (no `window-state.json`): loading window appears at the default centered 1200x800. Unchanged.
- Subsequent launches (any saved state exists): loading window appears at saved entry 0's bounds -- same x, y, width, height as the user's last-active window from the previous session. No visible jump when the backend comes up and content loads.
- If entry 0's workspace was deleted between sessions, `filterRestorableUrls` later drops it and the eventual content window settles at the next restorable entry's bounds. A small secondary jump may be visible in that edge case -- accepted.
- If the saved display is gone, `restoreWindowBounds()` already clamps to the primary display at a 50-px offset, so the loading window appears on the primary display rather than off-screen. No special handling needed.
- Unauthenticated users / users with nothing restorable also get the loading window at entry 0's saved bounds, as long as a state file exists. Welcome / create page then loads in that same window without a jump.
- Multi-window restore: the initial (MRU-zero) window keeps focus; the lesser-MRU windows appear at their saved bounds behind it. After restore, MRU order is preserved across the restart, so the next quit-and-relaunch cycle also lands the loading window on the user's last-active window.

## Implementation plan

All changes in `apps/minds/electron/main.js`. Several closely related sites.

- **Initial bounds (the core fix).** At the app-startup function around `main.js:1491`, after `initialBundle = createBundle()` and before `await runStartupSequence(initialBundle)`:
  - Call `loadSessionState()` once and store the result.
  - If the result is non-empty, call `restoreWindowBounds(initialBundle, savedState[0])`.

- **MRU-ordered persistence.** Change `saveSessionState` to iterate `mruWindows` instead of `bundles`, so entry 0 of the saved state is the user's most-recently-focused window. `mruWindows` is already maintained by the existing `focus` handler in `wireBundleWindowEvents` and the `closed` cleanup; no new tracking is needed.

- **Focus-preserving multi-window restore.**
  - Add a `showInactiveOnFirstShow` boolean field to the bundle constructor (default `false`).
  - In `wireBundleShowLogic`, factor the three show callbacks into a single `surface` closure that calls `win.showInactive()` if the flag is set, else `win.show()`.
  - Add a `{ showInactive = false } = {}` option to `openNewWindow` that sets `bundle.showInactiveOnFirstShow = true` when the caller asks for it.
  - In the multi-window restore loop in `runStartupSequence`, pass `{ showInactive: true }` when opening lesser-MRU windows, collect them, then rewrite `mruWindows` as `[initialBundle, ...restoredBundles]` to undo the unshift-reversal that `createBundle` would otherwise introduce.

## Implementation phases

1. **Apply saved bounds before the loading screen renders.** The 3-line addition in `onReady`.
2. **Persist session state in MRU order.** Single-line change in `saveSessionState`.
3. **Make multi-window restore focus-preserving.** Add the `showInactiveOnFirstShow` flag, the `openNewWindow` option, and the `mruWindows` rewrite at the restore site.
4. **Changelog and manual verification.**
   - Add `apps/minds/changelog/gleb-window-bug.md` per the repo's per-PR changelog policy.
   - Walk through the manual-verification scenarios below.

## Testing strategy

This is Electron main-process code; the Minds Electron startup sequence does not have automated tests in the repo, and adding a test harness for one helper is out of scope. Verification is manual.

- **Manual scenarios:**
  - Fresh install (delete `window-state.json` first): loading window appears at the default centered 1200x800. After backend is up, the welcome/create page renders. No jump. Unchanged behavior.
  - Saved bounds at non-default position: drag the window to a corner, resize, quit. On relaunch the loading window appears immediately at that location and size. No jump when content loads.
  - Multi-window state, single-monitor: open several windows, focus the second-to-last one last, quit. On relaunch the loading window appears at the bounds of the last-focused window, and the other windows restore behind it at their respective bounds.
  - Multi-window state, focus check: after restore completes, the front window is the one the user last interacted with -- the lesser-MRU windows do not steal focus during restore.
  - MRU persistence across restarts: quit, relaunch, focus a different window, quit, relaunch again. Loading window should appear at the new most-recently-focused window's bounds, not at the original one.
  - Deleted-workspace edge case: save multi-window state, delete the workspace corresponding to entry 0, relaunch. Loading window appears at entry 0's bounds; the content window then settles at the next restorable entry's bounds -- a small secondary jump is expected.
  - Display-gone case: save bounds on a secondary monitor, disconnect that monitor, relaunch. `restoreWindowBounds()`'s existing clamping should place the loading window on the primary display at a 50-px offset. No off-screen flash.
  - Unauthenticated start: with a saved state file but logged out, the loading window appears at entry 0's bounds and the welcome page loads in that window without a jump.
- **Code-level checks:**
  - No new TODO/FIXME comments.
  - No emojis.
  - No lint or type errors introduced.

## Open questions

- None of substance.
- Possible future improvement (out of scope): wire a `move` / `resize` listener on each window to debounce-save bounds during a session, so crash recovery picks up the latest position. Today bounds are only persisted on clean close / quit (`main.js:347-368`, `main.js:2147-2161`).
