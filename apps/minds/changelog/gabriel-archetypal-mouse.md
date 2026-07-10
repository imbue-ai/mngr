Persist the Electron main process's logs and recover gracefully when a workspace view's renderer crashes.

- The Electron main process now tees all of its console output (and any uncaught exception / unhandled rejection) into a new `~/.minds/logs/electron.log`, so main-process problems are durably diagnosable instead of vanishing in packaged builds.

- Both `electron.log` and the backend's `minds.log` now rotate at 100MB (keeping the 10 newest rotations, gzipped) instead of growing without bound.

- Bug reports upload the current `minds.log` and `electron.log` plus the most recent gzipped rotation of each, under accurate names (`backend_logs` / `electron_logs`), and a report filed while the backend is down now attaches both logs.

- When a workspace's content view crashes (e.g. its renderer is killed over a long sleep), the app now shows an "Aw, Snap!"-style crash page with the crash reason and a Reload button, rather than a blank white screen that only a manual Home-and-back would fix. Reload is manual (no auto-reload) to avoid crash loops.
