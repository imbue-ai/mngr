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
