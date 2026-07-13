# Plan: Electron main-process logging + crashed-content-view recovery page

## Refined prompt

Plan improvements from the blank-screen-after-sleep diagnosis (Sentry event bc6d78e995bd46738bc517644c9e9c23, kanjun's 2026-07-09 report):

* Persist Electron main-process logs to a new `electron.log` in `~/.minds/logs/` by teeing `console.log/warn/error` (timestamped) -- every existing call site captured, no rewrites
* Also log `uncaughtException` / `unhandledRejection` tracebacks to `electron.log` (log, then let default handling proceed) so main-process crashes are durably diagnosable
* Rotate BOTH `minds.log` and `electron.log` with a shared Electron-side rotation helper: 100MB threshold, keep 10 rotated files, gzip each rotated file after rotating (mirroring the jsonl scheme's sizes and timestamped-suffix naming)
* Do NOT touch the shared Python jsonl sink (`make_jsonl_file_sink` in `imbue_common`) -- jsonl rotation behavior is unchanged
* Error-report uploads include, per log, the current file plus the most recent rotation gzip; split the S3 attachment groups into accurate names (`backend_logs` for `minds.log`, `electron_logs` for `electron.log`)
* The Electron backend-down report path attaches both `minds.log` AND `electron.log` (today it attaches only the single newest `.log`), so a report filed while the backend is dead still shows why the backend died
* On content-view `render-process-gone`: log it, then show an Electron-local "Aw, Snap!"-style page in the content view with the crash reason/exit code, a Reload button (re-loads the pre-crash URL via the content-relay IPC bridge, which existing error pages already use), and a "Report a bug" button wired to the existing help flow
* No auto-reload (crash-loop safety, matching Chrome/Firefox/VS Code); the crash page is stateless -- repeated crashes show the same page each time
* Simple generic page styling to start (sad glyph + text + buttons); no pixel parity with the backend-served recovery pages
* Scope: content view only (chrome view, modal view, GPU `child-process-gone`, and `did-fail-load` handling are out of scope this round); `powerMonitor` suspend/resume logging is a noted follow-up

## Overview

* Kanjun's blank-screen-after-sleep incident (Sentry bc6d78e9) was undiagnosable from uploaded logs because the Electron main process logs nothing durable: all ~60 call sites are bare `console.*` that vanish in packaged builds, and `minds.log` (despite being uploaded as "electron_logs") contains only the Python backend's stdout/stderr.
* The likely mechanism -- the workspace content view's render process dying over sleep and leaving its pinned-white background -- is invisible and unrecoverable today: no `render-process-gone` handler exists, so the view stays white until the user manually navigates Home and back.
* Fix the observability gap with a new `electron.log` (console tee + uncaught-exception logging), rotated and gzipped alongside a newly-rotated `minds.log`, and uploaded through the existing bug-report S3 pipeline.
* Fix the recovery gap with a Chrome "Aw, Snap!"-style local crash page in the content view: manual Reload button rather than auto-reload, because if the page caused the crash auto-reload becomes a crash loop (this matches Chrome/Firefox/VS Code, and avoids retry-cap machinery entirely).
* Known Electron pitfall respected: never call `loadURL` synchronously inside the `render-process-gone` handler (electron#19887 -- it can crash the whole app); the crash-page navigation is deferred.
* Extend the recovery to the window's other two renderers (chrome and overlay views, which run in separate processes and can die independently over the same sleep), and close the observability gap that let the incident stay invisible: renderer deaths were never reported to Sentry (the SDK's default only breadcrumbs `killed`/`crashed`/`oom`; our handlers only logged), and the friendly crash page removes the user's motivation to file the manual report that surfaced it in the first place.

## Expected behavior

* Every `console.log/warn/error` from the Electron main process appears (timestamped, level-tagged) in `~/.minds/logs/electron.log`, in addition to stdout/stderr as today. Dev-terminal behavior is unchanged.
* Uncaught exceptions and unhandled rejections in the main process are written to `electron.log` with full stack traces before default handling (Sentry capture, process exit) proceeds unchanged.
* `minds.log` and `electron.log` each rotate at 100MB: the current file is renamed with a timestamp suffix, gzipped (e.g. `minds.log.20260709195046123456.gz`), and at most 10 rotated files per log are kept. No more unbounded 600MB log files.
* Bug reports (the `/help` flow with logs opted in) upload four log artifacts to S3 instead of two-ish today: current `minds.log`, current `electron.log` (each gzipped at upload), plus the most recent rotation gzip of each (uploaded once and cached, since rotated files are immutable). The Sentry event context shows them under accurate group names: `uploaded_files_backend_logs` / `uploaded_files_electron_logs` (+ `_rotated` variants), alongside the unchanged jsonl groups.
* Backend-down manual reports (the full-app error takeover) attach the tails of `minds.log`, `electron.log`, and the newest jsonl -- so a report filed while the backend is dead shows both why the backend died and what the shell did about it.
* When a workspace content view's renderer dies (any reason except `clean-exit`), the user sees a local "Aw, Snap!"-style page in the content area within a moment: sad glyph, "This workspace view crashed" with the reason and exit code, a Reload button, and a "Report a bug" button. The crash is also logged to `electron.log` with reason/exit code/URL.
* Clicking Reload re-loads the pre-crash workspace URL in the content view (spawning a fresh renderer). If it crashes again, the same page reappears -- stateless, the user is the loop-breaker.
* Clicking "Report a bug" opens the existing help/report modal (same flow the workspace-recovery page uses), scoped to the affected workspace.
* The content-view crash page occupies only the content view and is replaced by any subsequent navigation (e.g. the recovery flow or the user going Home); the window title, sidebar, and all backend-driven health/recovery flows are unchanged.
* When the chrome (titlebar) renderer dies, the user sees a miniaturized error strip in the titlebar with a Reload button instead of a blank white bar; the workspace content view keeps running, and Reload restores a fully-populated titlebar. When the overlay renderer dies it is silently reloaded warm, so the next sidebar/inbox/help open works normally.
* Abnormal renderer/child-process deaths (`crashed`, `oom`) are reported to Sentry automatically (subject to the existing `report_unexpected_errors` opt-in), labeled by which view died; sleep/external kills (`killed`) are deliberately not reported as events (unactionable, noisy) but remain in `electron.log` and any manual report.

## Changes

### Electron main-process logging (`apps/minds/electron/`)

* New logger module (e.g. `logger.js`): opens an append stream to `<logDir>/electron.log`, wraps `console.log/warn/error` to tee formatted, timestamped lines into it (original console behavior preserved), and registers `process.on('uncaughtException'/'unhandledRejection')` listeners that log the stack then leave default/Sentry handling untouched. Initialized as the very first thing in `main.js` (before `initSentry()`), so startup output is captured.
* New rotation helper (shared by `electron.log` and `minds.log`): on stream open and on a size check during writes, when the file exceeds 100MB rename it to `<name>.<YYYYMMDDHHMMSSffffff>` (matching the jsonl timestamp format), gzip it (then remove the uncompressed rename), and prune to the 10 newest rotated files. Only the Electron main process writes these files, so no cross-process locking is needed (unlike the Python sink).
* `backend.js`: route the existing `minds.log` write stream through the rotation helper instead of a bare `fs.createWriteStream`.

### Crash page for the content view (`apps/minds/electron/`)

* `main.js` `wireContentViewEvents`: track the last committed content URL per bundle (from the existing `did-navigate` handler), and add a `render-process-gone` handler that logs reason/exit code/URL, ignores `clean-exit`, and navigates the content view to the local crash page on a deferred tick (never synchronously -- electron#19887). Both content-view creation sites already call `wireContentViewEvents`, so post-retry views are covered automatically.
* New `crashed.html` (Electron-local, loaded into the content view so it runs the existing `content-relay-preload.js`): generic sad-glyph page rendering the reason/exit code passed via query params, with Reload and "Report a bug" buttons.
* Reload button: posts a new allowlisted message (e.g. `minds:reload-crashed-view`) relayed by `content-relay-preload.js` to a new `ipcMain` channel that re-loads the bundle's stored pre-crash URL -- same pattern as the existing `open-help` / `open-request-modal` relays.
* Report button: posts the existing `minds:open-help` message with the workspace agent id (passed to the page via query param), reusing the existing channel and validation unchanged.

### Upload plumbing

* `apps/minds/imbue/minds/utils/sentry/core.py`: replace the single mislabeled `electron_logs` (`*.log`) attachment group with four accurate groups -- `backend_logs` (`minds.log`, mutable, gzip-on-upload), `electron_logs` (`electron.log`, mutable, gzip-on-upload), and per-log rotated groups (`minds.log.*.gz` / `electron.log.*.gz`, `max_file_count=1`, immutable, `is_compressed=False` since they are pre-gzipped). Fix the stale layout comment while there. No changes to the shared uploader or the jsonl groups.
* `apps/minds/electron/sentry.js` `collectLogAttachments`: attach `minds.log` and `electron.log` explicitly (plus the newest jsonl, as today) instead of "the newest single `.log`", within the existing compressed-attachment budget.

### Chrome- and modal-view crash recovery (`apps/minds/electron/`)

The content view is not the only per-window renderer that can die over a long sleep. Each window bundles three `WebContentsView`s -- `chromeView` (the titlebar/menu chrome), `contentView` (the workspace), and `modalView` (the warm overlay host for the sidebar/inbox/help) -- and they run in *separate* renderer processes: the content view has its own session (partition `persist:workspace-content`) and origin, while the chrome and overlay views share the default session and backend origin. So the chrome renderer can die on its own, leaving a white, dead titlebar with **no** recovery affordance at all (worse than the content case, which at least gets a crash page with a button). Confirmed in practice: killing the chrome renderer leaves the workspace content running but the menu bar blank.

Recovery for these two views is deliberately *different* from the content view, because they host our own trusted, fixed first-party pages (`/_chrome`, `/_chrome/overlay`) rather than foreign workspace content, and because of how the views are layered.

* **Chrome view -- miniaturized in-titlebar error strip.** Add a `render-process-gone` handler on `chromeView` (ignore `clean-exit`, defer the navigation a tick per electron#19887, mirror the content handler's guards). The chrome view is a full-window *underlay*; the content view overlays it inset below the ~38px titlebar (`computeBundleViewBounds`), so when only the chrome renderer dies the **only visible region of the chrome view is the top titlebar strip**. The recovery UI therefore lives there:
  * New local `chrome-crashed.html` (loaded into the chrome view, which runs the full `preload.js` bridge): a compact single row anchored to the top titlebar strip -- brand maroon, a short message ("The menu bar stopped responding") and a prominent manual **Reload** button. No reason/exit-code detail (it won't fit, and that data already goes to `electron.log` + Sentry).
  * **Manual** Reload, not auto -- for the same loop-safety as the content view (if `/_chrome` itself is what crashes, the user is the loop-breaker) and for behavioral consistency across the two crash surfaces.
  * The chrome view is trusted first-party, so Reload calls a dedicated `reload-chrome` `ipcMain` channel directly (no content-relay indirection). The handler reloads `/_chrome`; the existing `did-finish-load` handler re-primes the fresh chrome from `latestChromeState`, so the bar returns fully populated.
  * Track a `bundle.isChromeCrashed` flag mirroring `isContentCrashed`, and leave the content view and its bounds untouched -- the workspace stays fully usable while the bar is being restored.

* **Modal/overlay view -- silent warm reload.** Add a `render-process-gone` handler on `modalView` (ignore `clean-exit`). This view is hidden whenever no overlay is open, so there is no visible page to show: just call the existing `loadOverlayHost(bundle)` to reload `/_chrome/overlay` warm, and reset any open-modal state (`closeModal`) so the next sidebar/inbox/help open lands on a fresh host. No new HTML.

Caveats to handle:

* On frameless platforms (Linux/Windows, `frame: false`) the window min/max/close controls are custom-drawn *inside* the chrome content, so they vanish with a dead chrome renderer until Reload restores them (macOS traffic lights are OS-drawn via `titleBarStyle: hiddenInset`, so they are unaffected). This is inherent to any chrome-renderer death; the prominent Reload button is the recovery path.
* Chrome dying *and* the backend being down at the same time is rare (two failures at once); keep the `reload-chrome` handler simple (attempt `/_chrome`; the existing backend-down detection/takeover already covers "backend is gone") rather than special-casing it.

### Sentry reporting of renderer and child-process deaths (`apps/minds/electron/sentry.js`)

The original blank-screen-after-sleep incident only reached us because a user hit a scary blank screen and filed a *manual* report; nothing was captured automatically. Two compounding gaps cause that, and the content-view crash page makes it worse (a friendly Reload page removes the user's motivation to report, so without automatic capture these incidents become *more* invisible):

1. `@sentry/electron`'s default `childProcess` integration (active via the default integrations, `DEFAULT_OPTIONS.events = ['abnormal-exit', 'launch-failed', 'integrity-failure']`) turns `killed`, `crashed`, and `oom` renderer deaths into **breadcrumbs only** -- never standalone events. A renderer reaped over sleep reports `reason=killed` (verified in staging `electron.log`: `reason=killed, exitCode=9`), so it is never sent.
2. Our own `render-process-gone` handlers only `console.error` -- they never call `Sentry.capture*`.

Fix, honoring the "only report what we can act on" filter:

* In `initSentry`, replace the default `childProcessIntegration()` with `childProcessIntegration({ events: [...defaults, 'crashed', 'oom'] })` so **`crashed` and `oom`** are captured as Sentry events across *all* processes (content/chrome/modal renderers plus GPU/utility). These are bug-shaped -- `crashed` is a real renderer fault, `oom` is frequently our own memory leak (especially in the first-party chrome view) -- so an individual event is diagnosable and a post-release rate spike is an actionable regression signal.
* **Do not** capture `killed` as an event. It is dominated by OS/sleep reaping and other external kills, is individually unactionable, and would be noisy. It stays a breadcrumb, still lands in `electron.log` (the `[content-crash]` / `[chrome-crash]` lines), and still uploads with any manual bug report -- so we are not blind to sleep-deaths, we just do not create unactionable Sentry issues for laptops going to sleep.
* Set `getRendererName(contents)` in `Sentry.init` (maps a `webContents` to its bundle view) so renderer-death events are labeled `chrome` / `content` / `modal` for triage.
* No new gating: the existing `beforeSend` already drops events when `report_unexpected_errors` is off, so these auto-captures honor the same opt-in.
* Optional enrichment (decide during implementation): additionally `Sentry.captureException`/`captureMessage` inside the content-view `render-process-gone` handler with the workspace URL/id, for richer per-workspace context than the app-global hook can attach. Baseline is the SDK-level broadening above.

### Acceptance criteria

* Killing the **content** renderer (`forcefullyCrashRenderer` / SIGKILL) still shows `crashed.html` with a working Reload (unchanged).
* Killing the **chrome** renderer shows the miniaturized titlebar error strip with a Reload button; the workspace content view keeps running; clicking Reload restores a fully-populated `/_chrome` bar. On macOS the traffic lights remain usable throughout.
* Killing the **modal/overlay** renderer is invisible while idle; opening the sidebar/inbox/help afterward works (the host silently reloaded).
* With `report_unexpected_errors` on: a `crashed` or `oom` renderer death produces a Sentry event labeled by view; a `killed` death produces **no** event (breadcrumb only) but still appears in `electron.log`.

### Follow-ups (out of scope, noted for later)

* `powerMonitor` suspend/resume logging (wake timestamps in `electron.log`).
* GPU/utility process crash *pages* (their deaths are now reported to Sentry via the broadened `childProcess` integration, but there is no user-facing recovery surface for them) and `did-fail-load` handling on the content view.
* Rotation for the Python jsonl logs' gzip story, if ever wanted -- deliberately untouched here to avoid changing shared `imbue_common` code.
