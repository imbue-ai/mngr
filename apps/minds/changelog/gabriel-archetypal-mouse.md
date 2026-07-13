Persist the Electron main process's logs and recover gracefully when a workspace view's renderer crashes.

- The Electron main process now tees all of its console output (and any uncaught exception / unhandled rejection) into a new `~/.minds/logs/electron.log`, so main-process problems are durably diagnosable instead of vanishing in packaged builds.

- Both `electron.log` and the backend's `minds.log` now rotate at 100MB (keeping the 10 newest rotations, gzipped) instead of growing without bound.

- Bug reports upload the current `minds.log` and `electron.log` plus the most recent gzipped rotation of each, under accurate names (`backend_logs` / `electron_logs`), and a report filed while the backend is down now attaches both logs.

- When a workspace's content view crashes (e.g. its renderer is killed over a long sleep), the app now shows an "Aw, Snap!"-style crash page with the crash reason and a Reload button, rather than a blank white screen that only a manual Home-and-back would fix. Reload is manual (no auto-reload) to avoid crash loops. The page uses a white background with the distressed Minds head logo and a "Bummer" heading.

- The window's other two views now recover from renderer death too: if the chrome (titlebar) renderer dies it shows a compact in-titlebar error strip with a Reload button (leaving the workspace content running) instead of a blank bar, and if the overlay/menu renderer dies it is silently reloaded so the next sidebar/inbox/help open works.

- Out-of-memory renderer deaths (`oom`, in any of the three views) are now reported to Sentry automatically (subject to the existing error-reporting opt-in) and labeled by which view died, so these failures are visible without waiting for a manual bug report. Native renderer crashes already flow to Sentry as minidumps, and sleep/external kills (`killed`) are deliberately left unreported (unactionable and noisy) but remain in `electron.log`.
