# Changelog - concurrency_group

A concise, human-friendly summary of changes for the `concurrency_group` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Optional `name` parameter on `RunningProcess` and the `run_process_to_completion` / `run_process_in_background` / `run_background` spawn APIs — a log-safe label used as the reader thread's name and as the display command in any `ProcessError` / `TimeoutExpired` raised. Callers whose command carries secret argument values (e.g. `--host-env PASSWORD=...`) can pass a masked/friendly `name` so those secrets never reach the JSONL log's `thread_name` field or an error message; the real argv still executes. Defaults to the joined command. `ProcessError` gains a `display_name` (and a `display_command` property) used for its rendered message while `.command` keeps the real argv; `FinishedProcess` gains a matching `display_name`.
- Added: Optional `pass_fds` parameter on `ConcurrencyGroup.run_process_in_background`, `run_background`, and `run_local_command_modern_version`, forwarded to `subprocess.Popen(pass_fds=...)` so callers can hand an already-connected `socketpair` endpoint to a child process without a rendezvous file on disk.

### Changed

- Changed: Force-terminate log message now states the reason explicitly ("it exceeded its <N>s timeout" or "a shutdown was requested (shutdown_event was set)"), so a routine cancellation is no longer misread as a timeout.

## [v0.1.20] - 2026-06-13

### Changed

- Changed: Concurrency-group exception handling now raises a dedicated custom exception type instead of `ValueError`.

## [v0.1.19] - 2026-06-05

## [v0.1.18] - 2026-05-28

## [v0.2.8] - 2026-05-13

### Changed

- Changed: Background processes started via `ConcurrencyGroup.run_process_in_background()` now default to `is_checked_by_group=True`, so non-zero exits surface as `ProcessError` at group teardown instead of being silently swallowed.
