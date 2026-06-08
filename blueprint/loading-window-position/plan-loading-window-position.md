# Plan: Loading-window position jump on startup

> When I start up the app the loading window appears, then when the app is ready it jumps to another spot. Make the loading screen window open at the same location/size as the (most relevant) window was at the last app close.
>
> * Apply the saved bounds at loading-window creation time — i.e. before `shell.html` is loaded into the chrome view — so there is no visible jump.
> * Position only the single initial loading window from saved bounds; additional saved windows continue to be created after the backend is up via the existing `openNewWindow` path.
> * Use saved entry index 0's bounds. If that workspace turns out to be unrestorable (its agent was deleted), accept a small secondary jump to entry 1's bounds when `restoreWindowBounds` runs later.
> * Apply saved bounds at the loading window whenever a state file exists, regardless of authentication or whether anything ends up restorable. Only a truly fresh install (no `window-state.json`) shows the default centered window.
> * Use raw saved x/y/width/height at window creation; do not re-implement display-availability logic at creation. If the saved monitor is gone, the existing `restoreWindowBounds` clamping at the later step still handles the fallback.
> * Restore both position and size at creation so the window doesn't resize after the loading screen, only x/y move.
> * Surface (but do not pre-decide) whether `saveSessionState` should iterate `mruWindows` instead of the `bundles` Set, so that "entry 0" means "the window the user most recently interacted with" rather than "the oldest-still-open window at quit time".

## Overview

- Bug: on startup, `createBundle()` builds the initial Electron `BaseWindow` with hard-coded 1200x800 and no x/y, so Electron centers it. The loading screen renders at that centered default. Only after the backend is up does `restoreWindowBounds()` move the window to the saved position — that's the visible jump.
- Fix: read `window-state.json` synchronously at the very top of the startup function, pull `{x, y, width, height}` from entry 0 (if any), and pass those bounds into `createBundle()` so the `BaseWindow` is constructed with the saved bounds from the start.
- Scope is intentionally narrow: one file (`apps/minds/electron/main.js`), one new optional parameter threaded through `createBundle` → `buildBundleWindowOptions`, one new call site that reads the state file once at startup.
- No refactor of `restoreWindowBounds`; off-screen-display fallback continues to live there. Additional restored windows are unchanged.
- One open decision to surface for the user: today saved-state ordering follows `bundles` insertion order (creation order), not MRU — entry 0 may not be the window the user was last using.

## Expected behavior

- First-ever launch (no `window-state.json`): loading window appears at the default centered 1200x800 position. Unchanged from today.
- Subsequent launches with a saved state file:
  - Loading window appears at the bounds of saved entry 0 (x, y, width, height). No jump when the backend comes up and content loads — restoration of entry 0 is a no-op against bounds that already match.
  - If the user is authenticated and entry 0 is restorable, the workspace loads into a window already at the correct bounds.
  - If the user ends up unauthenticated, or has no restorable workspaces: the welcome / create page loads into a window still at entry 0's saved bounds (today it would be at the default centered position).
  - If entry 0's workspace was deleted between quit and relaunch, `filterRestorableUrls` drops it and `restoreWindowBounds(initialBundle, entry1)` runs later — a small secondary jump from entry 0's bounds to entry 1's bounds is expected and accepted.
  - If the saved display is gone, the loading window first appears at the raw saved x/y (possibly partly or fully off-screen briefly), then the later `restoreWindowBounds` call clamps it onto the primary display.
- Additional saved windows beyond entry 0 are still opened later via `openNewWindow` after the backend is up, at their own saved bounds, unchanged from today.

## Implementation plan

All changes in `apps/minds/electron/main.js`.

- `buildBundleWindowOptions(initialBounds = null)` (currently at line 274):
  - Accept an optional `initialBounds` argument of the shape `{x, y, width, height}`.
  - When provided, override the default `width: 1200, height: 800` with the supplied dimensions and set `x` and `y` to the supplied values. When absent, behave exactly as today.
  - Do not validate the bounds here. Treat the caller as the source of truth.
- `createBundle(initialBounds = null)` (currently at line 394):
  - Accept an optional `initialBounds` argument.
  - Pass it through to `buildBundleWindowOptions(initialBounds)`.
  - All other logic unchanged (views, bundle object, `bundles` add, `mruWindows`, event wiring, show logic).
- App-startup function around line 1484 (the function that does `initialBundle = createBundle(); await runStartupSequence(initialBundle)`):
  - Call `loadSessionState()` once at the top of this function. Store the result in a local variable (e.g. `savedState`).
  - Extract the first entry (`savedState[0]` if `savedState.length > 0`).
  - If the first entry exists and has finite numeric `x`, `y`, `width`, `height`, build `initialBounds = { x, y, width, height }`; otherwise `initialBounds = null` (covers missing fields, fresh installs, malformed state).
  - Pass `initialBounds` into the `createBundle(initialBounds)` call.
  - Pass `savedState` into `runStartupSequence(initialBundle, savedState)` so it doesn't read the file a second time (see next bullet).
- `runStartupSequence(bundle, savedState = null)` (currently at line 1642):
  - Accept the optional `savedState` parameter.
  - At the existing line that calls `loadSessionState()` (line 1708), use the passed-in `savedState` if provided; otherwise call `loadSessionState()` as today.
  - All other logic unchanged. `restoreWindowBounds(initialBundle, first)` at line 1794 still runs; for the entry-0 case it sets the window to bounds that already match, which is a no-op from the user's perspective.
- `openNewWindow(url)` (currently at line 840): no change. Continues to call `createBundle()` with no arguments, which preserves today's behavior for additional windows.
- `restoreWindowBounds(bundle, entry)` (currently at line 1016): no change.
- `saveSessionState()` (currently at line 975): no change required by the fix itself. See Open questions for the optional MRU-ordering follow-up.

## Implementation phases

1. **Plumb the optional bounds through window creation.**
   - Update `buildBundleWindowOptions` to accept and merge `initialBounds`.
   - Update `createBundle` to accept and forward `initialBounds`.
   - No behavioral change yet — every existing call site still passes nothing. System remains working.

2. **Apply saved bounds at the initial loading window.**
   - In the app-startup function, call `loadSessionState()`, derive `initialBounds` from entry 0, pass it to `createBundle`.
   - At this point the bug is fixed for the common case. The state file is read twice (once here, once inside `runStartupSequence`) — acceptable but wasteful.

3. **Pass loaded state into `runStartupSequence` to avoid the redundant read.**
   - Thread `savedState` from the startup function into `runStartupSequence` as an optional parameter; reuse it instead of reloading.

4. **Add the per-project changelog entry and run manual verification.**
   - Create `apps/minds/changelog/gleb-window-bug.md` (branch is `gleb-window-bug`, slashes-to-dashes).
   - Manually verify the scenarios listed in Testing strategy.

## Testing strategy

This is Electron main-process code; the Minds Electron startup sequence does not have automated tests in the repo. Verification is primarily manual.

- **Manual scenarios to verify:**
  - Fresh install (delete `window-state.json` first): loading window appears at the default centered 1200x800. After backend is up, the welcome/create page renders. No jump.
  - Single saved window at non-default bounds (e.g. drag to a corner, resize, then quit): on relaunch the loading window appears immediately at that location and size. No jump when content loads.
  - Two or more saved windows: the initial loading window appears at entry 0's bounds. After backend is up, additional windows open at their respective saved bounds.
  - Deleted-workspace case: save state with multiple windows, delete one workspace via another client, then relaunch. Loading window appears at entry 0's bounds. If entry 0's workspace was the one deleted, the visible content window settles at entry 1's bounds — a small secondary jump, as accepted.
  - Display-gone case: save bounds on a secondary monitor, disconnect that monitor, then relaunch. Loading window initially appears at raw saved x/y (may be off-screen for a moment), then `restoreWindowBounds` clamps it to the primary display once the backend is up. Document this observable secondary movement as expected.
  - Unauthenticated start: with a saved state file but logged out, the loading window still appears at entry 0's bounds and the welcome page loads in that same window without a jump.
- **Code-level checks:**
  - No new TODO/FIXME comments.
  - No emojis.
  - No type errors / lint failures in `apps/minds/electron/main.js`.
- **Unit/integration test seams:** the natural pure-function seam is `buildBundleWindowOptions(initialBounds)`. Writing a JS unit test for this single file would require setting up a test runner that does not currently exist in this directory. Recommendation: do not add a new test harness for this one helper. If the user wants automated coverage, that is a separate plan.

## Open questions

- **Should `saveSessionState` iterate `mruWindows` instead of the `bundles` Set?** Today saved entry 0 is the oldest-still-open window at quit time, not the most-recently-active. The user noted this ambiguity during Q&A. Options:
  - Leave `saveSessionState` as-is. Entry 0 stays "oldest still open." Simpler, but the loading window's bounds may not match what the user remembers using last.
  - Change `saveSessionState` to iterate `mruWindows` (most-recent first), so entry 0 is the window the user last focused. Loading window appears where the user last interacted. Tiny added complexity; need to verify `mruWindows` is consistent at quit time on all the close paths (`before-quit` vs per-window `close`).
- **Should the loading window be skipped (not bounded) when the saved state is unusually stale?** E.g. multiple OS user-switches, dock-relocated displays. The current plan only checks for finite numeric fields, not for "saved bounds plausibly still on a visible display." Q4 deliberately chose to leave display-validation to the later `restoreWindowBounds` step; surface this as a known trade-off rather than re-litigate.
- **First-launch when only a `welcome`/`/` URL would have been saved:** today saved entries with `toRelativeBackendUrl(null)` return null and are skipped in `saveSessionState`, so a brand-new user who quits before completing onboarding may not write any state at all. Worth verifying during manual testing — not a code change in this plan, just a behavior to confirm.
