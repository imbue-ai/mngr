# Plan: Loading-window position jump on startup

> When I start up the app the loading window appears, then when the app is ready it jumps to another spot. Make the loading screen window open at the same location as the previous session.
>
> * Reuse the existing `restoreWindowBounds()` helper to apply saved bounds to the initial window before its loading screen renders.
> * Just grab any existing entry from the saved state (entry 0) — don't care whether it's MRU or creation order; any prior position is better than the centered default.
> * Apply at the existing app-startup site as a tiny addition after `createBundle()` — no plumbing through `createBundle` / `buildBundleWindowOptions`, no new helpers.
> * First-ever launch (no `window-state.json`) keeps the default centered 1200x800.

## Overview

- Bug: `createBundle()` constructs the initial `BaseWindow` with default 1200x800 and no x/y, so Electron centers it. The loading screen renders at that centered default. Once the backend is up, `restoreWindowBounds()` moves the window — that's the visible jump.
- Fix: between `createBundle()` and `runStartupSequence()` at the existing startup site (around `main.js:1484`), load the saved state and call `restoreWindowBounds()` on the new window with the first entry. The initial `BaseWindow` is created with `show: false` (`main.js:281`), so `setBounds` applied before `show()` is invisible to the user — no flash at the default position.
- Scope: ~3 lines in one file. Everything else (saving, loading, multi-window restore, display-gone fallback) is reused as-is.

## Expected behavior

- First-ever launch (no `window-state.json`): loading window appears at the default centered 1200x800. Unchanged.
- Subsequent launches (any saved state exists): loading window appears at saved entry 0's bounds — same x, y, width, height as that prior window. No visible jump when the backend comes up and content loads.
- If entry 0's workspace was deleted between sessions, `filterRestorableUrls` later drops it and the eventual content window settles at entry 1's bounds. A small secondary jump may be visible in that edge case — accepted.
- If the saved display is gone, `restoreWindowBounds()` already clamps to the primary display at a 50-px offset, so the loading window appears on the primary display rather than off-screen. No special handling needed.
- Unauthenticated users / users with nothing restorable also get the loading window at entry 0's saved bounds, as long as a state file exists. Welcome / create page then loads in that same window without a jump.
- Additional saved windows beyond entry 0 are still opened later via `openNewWindow` after the backend is up, at their own saved bounds. Unchanged.

## Implementation plan

All changes in `apps/minds/electron/main.js`. One file, one site.

- At the app-startup function around `main.js:1484`, after `initialBundle = createBundle()` and before `await runStartupSequence(initialBundle)`:
  - Call `loadSessionState()` once and store the result.
  - If the result is non-empty, call `restoreWindowBounds(initialBundle, savedState[0])`.
- No other code changes. `createBundle`, `buildBundleWindowOptions`, `restoreWindowBounds`, `loadSessionState`, `saveSessionState`, `openNewWindow`, and `runStartupSequence` are all untouched.

Concretely, the diff is roughly:

```js
initialBundle = createBundle();
const savedState = loadSessionState();
if (savedState.length > 0) restoreWindowBounds(initialBundle, savedState[0]);
await runStartupSequence(initialBundle);
```

Notes:
- `loadSessionState()` will run twice during startup (once here, once inside `runStartupSequence` at the existing line ~1708). It is a synchronous read of a tiny JSON file — the cost is negligible and not worth a refactor to thread state through.
- `restoreWindowBounds()` is safe to call on a hidden window (`show: false`); the bounds simply apply for when `show()` is later triggered by the chrome view's `did-finish-load`.

## Implementation phases

Effectively one phase, but split for clarity:

1. **Apply saved bounds before the loading screen renders.**
   - Insert the `loadSessionState()` call and the `restoreWindowBounds(initialBundle, savedState[0])` call at the startup site.
2. **Changelog and manual verification.**
   - Add `apps/minds/changelog/gleb-window-bug.md` per the repo's per-PR changelog policy.
   - Walk through the manual-verification scenarios below.

## Testing strategy

This is Electron main-process code; the Minds Electron startup sequence does not have automated tests in the repo, and adding a test harness for one helper is out of scope. Verification is manual.

- **Manual scenarios:**
  - Fresh install (delete `window-state.json` first): loading window appears at the default centered 1200x800. After backend is up, the welcome/create page renders. No jump. Unchanged behavior.
  - Saved bounds at non-default position: drag the window to a corner, resize, quit. On relaunch the loading window appears immediately at that location and size. No jump when content loads.
  - Multi-window state: more than one saved entry. The initial loading window appears at entry 0's bounds; additional windows open at their respective bounds once the backend is up.
  - Deleted-workspace edge case: save multi-window state, delete the workspace corresponding to entry 0, relaunch. Loading window appears at entry 0's bounds; the content window then settles at entry 1's bounds — a small secondary jump is expected.
  - Display-gone case: save bounds on a secondary monitor, disconnect that monitor, relaunch. `restoreWindowBounds()`'s existing clamping should place the loading window on the primary display at a 50-px offset. No off-screen flash.
  - Unauthenticated start: with a saved state file but logged out, the loading window appears at entry 0's bounds and the welcome page loads in that window without a jump.
- **Code-level checks:**
  - No new TODO/FIXME comments.
  - No emojis.
  - No lint or type errors introduced.

## Open questions

- None of substance. The MRU-vs-creation-order question is dropped: any saved position is acceptable for the loading window, so we just take entry 0 as-is.
- Possible future improvement (out of scope): wire a `move` / `resize` listener on each window to debounce-save bounds during a session, so crash recovery picks up the latest position. Today bounds are only persisted on clean close / quit (`main.js:347-368`, `main.js:2147-2161`).
