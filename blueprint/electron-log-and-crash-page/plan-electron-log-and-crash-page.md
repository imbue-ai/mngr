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

## Expected behavior

* Every `console.log/warn/error` from the Electron main process appears (timestamped, level-tagged) in `~/.minds/logs/electron.log`, in addition to stdout/stderr as today. Dev-terminal behavior is unchanged.
* Uncaught exceptions and unhandled rejections in the main process are written to `electron.log` with full stack traces before default handling (Sentry capture, process exit) proceeds unchanged.
* `minds.log` and `electron.log` each rotate at 100MB: the current file is renamed with a timestamp suffix, gzipped (e.g. `minds.log.20260709195046123456.gz`), and at most 10 rotated files per log are kept. No more unbounded 600MB log files.
* Bug reports (the `/help` flow with logs opted in) upload four log artifacts to S3 instead of two-ish today: current `minds.log`, current `electron.log` (each gzipped at upload), plus the most recent rotation gzip of each (uploaded once and cached, since rotated files are immutable). The Sentry event context shows them under accurate group names: `uploaded_files_backend_logs` / `uploaded_files_electron_logs` (+ `_rotated` variants), alongside the unchanged jsonl groups.
* Backend-down manual reports (the full-app error takeover) attach the tails of `minds.log`, `electron.log`, and the newest jsonl -- so a report filed while the backend is dead shows both why the backend died and what the shell did about it.
* When a workspace content view's renderer dies (any reason except `clean-exit`), the user sees a local "Aw, Snap!"-style page in the content area within a moment: sad glyph, "This workspace view crashed" with the reason and exit code, a Reload button, and a "Report a bug" button. The crash is also logged to `electron.log` with reason/exit code/URL.
* Clicking Reload re-loads the pre-crash workspace URL in the content view (spawning a fresh renderer). If it crashes again, the same page reappears -- stateless, the user is the loop-breaker.
* Clicking "Report a bug" opens the existing help/report modal (same flow the workspace-recovery page uses), scoped to the affected workspace.
* The chrome view, modal view, window title, sidebar, and all backend-driven health/recovery flows behave exactly as today; the crash page only occupies the content view and is replaced by any subsequent navigation (e.g. the recovery flow or the user going Home).

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

### Follow-ups (out of scope, noted for later)

* `powerMonitor` suspend/resume logging (wake timestamps in `electron.log`).
* Crash handling for the chrome view, modal view, GPU process (`app.on('child-process-gone')`), and `did-fail-load` on the content view.
* Rotation for the Python jsonl logs' gzip story, if ever wanted -- deliberately untouched here to avoid changing shared `imbue_common` code.
