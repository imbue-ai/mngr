# Unabridged Changelog - concurrency_group

Full, unedited changelog entries consolidated nightly from individual files in `libs/concurrency_group/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-22

Clarified the log message emitted when a subprocess is force-terminated. The old wording ("Aborting command (via sigterm to <pid>) due to signal...") was logged identically whether the command hit its own timeout or was cancelled by a requested shutdown, which made it easy to misread a routine cancellation as a timeout. It now states the reason explicitly -- either "it exceeded its <N>s timeout" or "a shutdown was requested (shutdown_event was set)". The command's argv is still deliberately omitted (this generic runner is used by callers that pass secrets in argv), so only the pid is logged.

## 2026-06-19

Added an optional `pass_fds` parameter to `ConcurrencyGroup.run_process_in_background`, `run_background`, and `run_local_command_modern_version`. It forwards to `subprocess.Popen(pass_fds=...)`, keeping the given file descriptors open in (and inheritable by) the spawned child. This lets callers hand an already-connected `socketpair` endpoint to a child process without a rendezvous file on disk. Defaults to empty, so existing behavior is unchanged.

## 2026-06-11

Replaced a direct ValueError raise in concurrency group exception handling with a dedicated custom exception type.

## 2026-06-10

Hardened the concurrency_group test suite (no production behavior change):

- `test_executor_respects_max_workers` now asserts that two workers actually run concurrently (`== 2`) and fails loudly if the synchronizing barrier breaks, instead of only checking the upper bound.
- `test_run_background_thread_safety` now blocks the subprocess on a signal file so the concurrent poll/read access is genuinely exercised while the process is running, and asserts on the observed output rather than only "no errors".
- `test_run_background_interleaved_stdout_stderr` now asserts per-stream line order directly instead of sorting it away.
- The suppressed/unchecked failed-thread tests now confirm the failing thread actually ran, so they can no longer pass vacuously.
- `test_all_failure_modes_get_combined` no longer pins a timing-dependent exact exception count; it asserts the failure kinds that must always be present and polls for the killed process to be reaped.
- Removed flaky wall-clock upper-bound timing assertions from `test_run_background_real_time_queue` and `test_concurrency_group_does_not_raise_when_within_timeout`.
- `test_nesting_in_the_same_thread_just_works` now asserts an observable effect (inner group exits, inner thread runs) instead of only not raising.
- Collapsed two duplicate `_shutdown_popen` tests into one that verifies the SIGTERM returncode.
- Added clarifying comments to the strand-cleanup tests, removed unused `tmp_path` parameters, switched `test_run_background_with_cwd` to the `tmp_path` fixture, and moved long-lived placeholder subprocesses to a single globally-unique sleep duration (`LONG_SLEEP_SECONDS`).

Raised the stale coverage floor from 90% to 95% to match the coverage CI already measures (~96%).

## 2026-06-04

Adopted the new repo-wide `per-file host uploads inside loops` ratchet check (flags write_file/write_text_file/put_file calls inside loops, which should use a single rsync via host.copy_directory instead). No production code change in this project.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# ty 0.0.39 type fixes

- Converted bracketed `# type: ignore[...]` suppressions to `# ty: ignore[...]`, as required by `ty` 0.0.39.
- Reworked the exit-path exception handling in `ConcurrencyGroup` to accumulate a typed `list[Exception]` (the non-`Exception` `BaseException`s are still re-raised exactly as before) so that the `_deduplicate_exceptions` call type-checks under the stricter checker. Behavior is unchanged.

- Tightened this project's `test_ratchets.py` violation counts to their exact current values (`--inline-snapshot=trim`).

No user-facing behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

## 2026-05-13

Background processes started with `ConcurrencyGroup.run_process_in_background()` now default to `is_checked_by_group=True`, so non-zero exits surface as `ProcessError` at group teardown instead of being silently swallowed. Pass `is_checked_by_group=False` for processes the caller terminates explicitly (e.g. via `terminate()` or a fire-and-forget timeout).
