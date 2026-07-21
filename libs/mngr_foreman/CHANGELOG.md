# Changelog - mngr_foreman

A concise, human-friendly summary of changes for the `mngr_foreman` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added `mngr_foreman`, an always-on web remote-control plugin for mngr agents. `mngr foreman` runs a single Flask server over every agent in mngr's view: a mobile-friendly chat UI for claude agents (live transcript with markdown, syntax highlighting, KaTeX, mermaid, inline images and file uploads; send messages; interrupt) and a web terminal (xterm.js over a pty bridge) for any agent type. A warm SSH connection pool keeps resolution and connections hot for fast sends/reads. No code is deployed to target boxes and there is no auth by design — bind to a private network. Third-party frontend libraries (marked, xterm, highlight.js, KaTeX, mermaid, Atkinson Hyperlegible fonts) are pinned by exact URL + sha256 and fetched into a local cache on first run instead of being vendored in the repo, so the package stays small; an offline box degrades gracefully (markdown falls back to escaped text, and syntax highlighting/math/diagrams/custom fonts stay off).
- Added `mngr foreman -d`/`--background`: run the server detached in the background. It prints `foreman started (PID <pid>) on http://<host>:<port>/  — logs: <logfile>  stop: kill <pid>` and returns immediately (exit 0) while the server keeps serving. The daemon re-execs `mngr foreman` into its own session (`setsid`, no controlling tty) with stdout+stderr redirected to `--foreman-log-file` (default `~/.mngr/foreman.log`; named `--foreman-log-file` because `--log-file` is already mngr's structured-log flag), so it survives the terminal closing and skips the parent-death watcher that kills a foreground server when its launcher exits. A `--pid-file` (default `~/.mngr/foreman.pid`) guards against a second copy — `-d` refuses to start when a live PID is recorded there (`foreman already running (PID <pid>)`, exit 1) and ignores a stale one. Frontend assets are fetched before forking so a slow/failing first-run download stays visible on the terminal.
