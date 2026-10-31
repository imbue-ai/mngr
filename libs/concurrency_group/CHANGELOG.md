# Changelog - concurrency_group

A concise, human-friendly summary of changes for the `concurrency_group` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.22] - 2026-10-28

### Added

- Added: `RunningProcess.get_reader_thread_stack()` returns the reader thread's `module.function` names (no paths, arguments, or locals) for diagnosing a `terminate()` that timed out.

### Fixed

- Fixed: Stopping a subprocess no longer raises when the OS refuses the stop signal (e.g. macOS "Operation not permitted" on a child mid-exit); the refusal is logged and the shutdown waits for the exit and falls back to SIGKILL as usual.

## [v0.1.21] - 2026-10-22

### Added

- Added: Optional `name` parameter on `RunningProcess` and the `run_process_to_completion` / `run_process_in_background` / `run_background` spawn APIs — a log-safe label used as the reader thread's name and as the display command in any `ProcessError` / `TimeoutExpired` raised. Callers whose command carries secret argument values (e.g. `--host-env PASSWORD=...`) can pass a masked/friendly `name` so those secrets never reach the JSONL log's `thread_name` field or an error message; the real argv still executes. Defaults to the joined command. `ProcessError` gains a `display_name` (and a `display_command` property) used for its rendered message while `.command` keeps the real argv; `FinishedProcess` gains a matching `display_name`.
- Added: Optional `pass_fds` parameter on `ConcurrencyGroup.run_process_in_background`, `run_background`, and `run_local_command_modern_version`, forwarded to `subprocess.Popen(pass_fds=...)` so callers can hand an already-connected `socketpair` endpoint to a child process without a rendezvous file on disk.
- Added: `stdin_bytes` on `ConcurrencyGroup.run_process_to_completion` / `run_process_in_background`: bytes written to the child's stdin, which is then closed, so a secret can reach a command without appearing in a process listing.
- Added: `is_output_accumulated` (default `True`) on `run_process_in_background()`, `run_background()` and `run_local_command_modern_version()`; `False` streams output to the callback/queue without retaining it, and reading it back raises `OutputNotAccumulatedError`.
- Added: `shutdown_timeout_sec` on `run_process_in_background` (default 30s), the grace between SIGTERM and SIGKILL when the process is stopped.
- Added: `monotonic_fn` on `run_local_command_modern_version`, the clock its `timeout` is measured against (defaults to the real monotonic clock).

### Changed

- Changed: Waiting on an idle background process no longer costs CPU: the runner blocks until output, exit, shutdown or timeout instead of polling each child 100 times a second. `ShutdownEvent` propagates `set()` down its tree, gains a `call_on_set()` context manager, and `wait(timeout=0)` on an already-set event now returns `True`. `run_background`, `RunningProcess` and `run_local_command_modern_version` take a `ShutdownEvent` instead of a `threading.Event`.

### Removed

- Removed: `CompoundEvent`, the `MutableEvent` alias, `ShutdownEvent.from_parent`'s `external` argument, the `shutdown_event` argument of `run_process_in_background` / `run_process_to_completion`, `poll_time` on `run_local_command_modern_version`, and `RunningProcess.stream_stdout_and_stderr()` / `get_queue()`. `run_background()` no longer allocates an `output_queue` when none is passed.

### Fixed

- Fixed: Subprocess shutdown logging no longer misreports routine cleanup as a forced kill. A process that already exited by the time its shutdown is requested (the common case for single-use workers) is reaped quietly with a debug line stating its actual exit code, instead of logging a SIGTERM. When a live process is genuinely stopped, the log message now states the reason explicitly ("Stopping subprocess (pid ...) with SIGTERM because it exceeded its <N>s timeout" or "... because the parent requested cleanup (shutdown_event was set)"), so a routine cancellation is no longer misread as a timeout.
- Fixed: Command timeouts use a monotonic deadline, so time the machine spent suspended no longer counts against a command.
- Fixed: A process killed on timeout or parent shutdown now has its final output (drained for up to two seconds) in the returned `FinishedProcess` instead of losing it.

## [v0.1.20] - 2026-06-13

### Changed

- Changed: Concurrency-group exception handling now raises a dedicated custom exception type instead of `ValueError`.

## [v0.1.19] - 2026-06-05

## [v0.1.18] - 2026-05-28

## [v0.2.8] - 2026-05-13

### Changed

- Changed: Background processes started via `ConcurrencyGroup.run_process_in_background()` now default to `is_checked_by_group=True`, so non-zero exits surface as `ProcessError` at group teardown instead of being silently swallowed.
