# Unabridged Changelog - concurrency_group

Full, unedited changelog entries consolidated nightly from individual files in `libs/concurrency_group/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-10-26

Stopping a subprocess no longer fails when the operating system refuses the stop signal. macOS can refuse a SIGTERM to our own child with "Operation not permitted" while that child is in the middle of exiting, and the refusal used to escape the shutdown as an exception: the wait for the child's exit and the SIGKILL fallback were skipped, and the error propagated to whoever was stopping the process (in Imbue Studio, it turned a routine pull timeout into a failed machine-record sync pass for every account). A refused signal is now logged with the pid and the shutdown waits for the exit as usual; a child that refuses both SIGTERM and SIGKILL and never exits is still reported through the existing "Process didn't die after kill()" error.

## 2026-10-13

The group headers in `test_ratchets.py` change from the banner form `# --- Code safety ---` to `# ratchet_group("Code safety")`, a marker the ratchet sync script reads. The script now also keeps every test under the group its check has in `standard_ratchet_checks.py`, and the groups in that file's order. Here that moved the `Modal images` group up to its place after `Process management`. No ratchet, recorded count, or test name changes.

## 2026-10-07

A command given `stdin_bytes` that exits without reading all of its input is now reported by its own exit code and output. Before, if the command exited before its input was fully written, the write's broken pipe was reported as the command failing to start (`ProcessSetupError: Command failed to start ... [Errno 32] Broken pipe`), and the command's exit code and error output were lost. A logged-out `claude -p`, which prints "Not logged in" and exits without reading its prompt, sometimes surfaced as "could not run: Broken pipe" instead of "Not logged in".

## 2026-10-05

Adopted the new repo-wide `raw ConcurrencyGroup.start_new_thread calls` ratchet (`test_prevent_raw_start_new_thread`). It flags production code that calls `ConcurrencyGroup.start_new_thread` directly instead of `start_mngr_thread`, the mngr helper that destroys the thread's gevent Hub when its target returns. No production code change in this project.

## 2026-09-29

Tests only; the library behaves as before.

The guard that keeps waiting on idle background children from costing CPU turned CI red a few times a day (MIND-333). It compared a CPU measurement against 2% of a ~2.4s wait, which is about five ticks of the clock it reads: CI runs in a gVisor sandbox, which charges CPU in 10ms ticks to whichever thread is running when one fires, so a wait that really costs 2-4ms is charged anywhere from 0 to 0.12s. The budget sat inside that noise, and 20 runs of the guard in such a sandbox failed 1-2 times when idle and 6-7 times under CPU contention.

- A new unit test counts how often the read loop wakes while a child sits idle, rather than what the wakeups cost. The count is an exact integer, comes out the same whether the child idles for 0.53s or 2.37s, and was 6 or 9 across 140 runs on macOS and in a Modal sandbox, idle and loaded. Reintroducing the 10ms poll the loop replaced takes it to 159.

- The end-to-end CPU guard now has an absolute ceiling of 0.25 CPU-seconds instead of a 2% share of the wait. That is about twice the worst measurement healthy code has ever produced, and about half the cheapest measurement the polling implementation produced, so the guard keeps its breadth over any route that burns idle CPU while sitting outside the sampling noise. It no longer needs `@pytest.mark.flaky`.

## 2026-09-27

`run_local_command_modern_version` now accepts the clock its `timeout` is measured against, as `monotonic_fn`. It defaults to the real monotonic clock, so nothing about running a command changes.

The reason is that a command's deadline starts at the moment the process is spawned, and nothing synchronises the child's startup against it. A test that needs the deadline to arrive at a particular point in the child's life -- once it has installed a signal handler, say -- had no way to ask for that, so it had to guess a deadline generous enough for the child to get there first. Under load the guess lost: the test that covers a timed-out command's last words killed a child that had not finished starting, and failed on an empty stdout. Handing the test the clock removes the guess -- it expires the deadline itself, the moment the child's own output says it is ready -- so the test now holds for any deadline, from a millisecond upwards, instead of being a bet on how busy the machine is.

The bound on draining a killed child's pipes is deliberately left on the real clock, so a posed deadline cannot leave that drain unbounded.

## 2026-09-23

Waiting on a background process no longer costs CPU while it is idle. Previously every running child held a thread that woke 100 times a second to check for exit and read its (usually empty) output pipes, and `ShutdownEvent.wait` polled every 10 ms on top of that. Eight idle children cost about 6.5% of a core on an M-series Mac, and more under gVisor. This was most of the idle CPU of long-lived streamers such as `mngr observe` (issue #700). The runner now blocks until the child writes output, exits, is asked to shut down, or times out, and eight idle children measure 0.00%.

- `ShutdownEvent` now propagates `set()` down its tree instead of each waiter polling its ancestors, so `wait()` is a single blocking wait. A new `call_on_set()` context manager runs a callback when the event is set. `wait(timeout=0)` on an already-set event now returns `True`; it used to return `False`.

- Removed `CompoundEvent`, the `MutableEvent` alias, `ShutdownEvent.from_parent`'s `external` argument, and the `shutdown_event` argument of `ConcurrencyGroup.run_process_in_background` / `run_process_to_completion`. Nothing in the repo used them. `run_background`, `RunningProcess` and `run_local_command_modern_version` now take a `ShutdownEvent` rather than a plain `threading.Event`, and `run_local_command_modern_version` no longer takes `poll_time`.

## 2026-09-21

Adopted the new repo-wide `raw ConcurrencyGroupExecutor construction` ratchet (`test_prevent_raw_concurrency_group_executor`). It flags production code that builds a `ConcurrencyGroupExecutor` directly instead of using `mngr_executor`, the wrapper that destroys each worker thread's gevent Hub. No production code change in this project.

## 2026-09-18

`ConcurrencyGroup.run_process_in_background` takes `shutdown_timeout_sec`, the grace a process gets between SIGTERM and SIGKILL when it is stopped (30 seconds by default). A caller whose process has nothing worth saving on exit can pass a short grace so that stopping it stays quick on a loaded machine.

## 2026-09-07

`ConcurrencyGroup.run_process_to_completion` and `run_process_in_background` accept `stdin_bytes`: bytes handed to the child on standard input, which is then closed. This is how a value that must not appear in a process listing (a secret, for instance) reaches a command that reads it from stdin. Without it, children keep seeing an empty stdin as before.

## 2026-08-24

A process killed on timeout (or parent shutdown) now has its final output drained into the returned `FinishedProcess`: whatever the child wrote between the last poll and its death -- including anything printed while handling the shutdown signal -- was previously lost. This tail is what diagnoses where a timed-out command was stuck (minds' recovery `mngr start` timeouts being the motivating case). The drain is bounded to two seconds, so a grandchild that inherited the pipes and is still writing cannot hold the kill path open.

## 2026-08-21

A command with a timeout is no longer killed for time the machine spent suspended. The deadline was measured on the wall clock, which keeps running while a laptop sleeps -- and the process that would notice the deadline is frozen along with the machine, so the whole budget could be spent without the command getting any further. A workspace restart caught by two fifteen-minute sleeps burned its twenty-one minute cap in a few hundred seconds of running time, and was reported as having timed out on the wake. The deadline is now monotonic, so only time the command was actually running counts against it.

## 2026-08-03

Merge main into the mithril-refactor stack (relay-based sharing, iframe desktop shell, Mithril SPA). The changes to this project arrive verbatim from main and are described by their original PRs' changelog entries; no project-specific conflict resolution was needed here.

No code changes in this project. This PR lands the mithril-stack rewrite (relay-based sharing, single-iframe Electron shell, mithril SPA) into main; for this project that means only the per-PR changelog entries accumulated on the stack's development branches.

## 2026-08-02

Added the repo-wide `test_prevent_unpinned_modal_pip_install` ratchet (synced into every project's `test_ratchets.py`): Modal images must install from a committed hash-locked requirements export instead of passing bare package names to `pip_install`/`uv_pip_install`. No violations in this project (count 0).

## 2026-07-29

Added an `is_output_accumulated` option (default `True`, so existing behavior is unchanged) to `ConcurrencyGroup.run_process_in_background()`, `run_background()` and `run_local_command_modern_version()`. Passing `False` means the process's output is not retained at all -- it is handed to the `on_output` callback / output queue as it arrives and then dropped, at both layers that used to keep a full copy: the decoded per-line lists on `RunningProcess`, and the raw byte buffer in `PartialOutputContainer` that backs `FinishedProcess.stdout`.

This is for long-running processes that supervise a streaming child (e.g. `mngr latchkey forward`, whose `mngr observe` child emits a discovery snapshot every 30s for the life of the process). Previously the parent retained every line the child ever printed, growing without bound. There is deliberately no size cap: a short-lived process with large output should keep all of it, so the caller states its intent explicitly instead.

Reading output back from a non-accumulating process raises `OutputNotAccumulatedError` rather than returning an empty string, so the situation cannot be misread as "the process printed nothing". Errors raised about such a process (timeouts, non-zero exits) report an explicit placeholder in place of its output.

Also removed two unused methods from `RunningProcess`: `stream_stdout_and_stderr()` and `get_queue()`. Neither had any caller. Consequently `run_background()` no longer allocates an `output_queue` when the caller does not supply one -- with no way to read that queue back, it could never be drained and merely accumulated an entry per output line. Callers that pass their own queue are unaffected.

## 2026-07-21

Subprocess shutdown logging no longer misreports routine cleanup as a forced kill. A process that already exited by the time its shutdown is requested (the common case for single-use workers, e.g. the minds warm `mngr` processes) is now reaped quietly with a debug line stating its actual exit code, instead of logging "Terminating subprocess ... with SIGTERM because a shutdown was requested". A process that is genuinely still alive logs "Stopping subprocess (pid ...) with SIGTERM because the parent requested cleanup (shutdown_event was set)"; the timeout path keeps its explicit "exceeded its Ns timeout" wording.

## 2026-07-08

`RunningProcess` (and the `run_process_to_completion` / `run_process_in_background` / `run_background` spawn APIs) now accept an optional `name`: a log-safe label used as the reader thread's name and as the display command in any `ProcessError` / `TimeoutExpired` it raises. Callers whose command carries secret argument values (e.g. `--host-env PASSWORD=...`) can pass a masked/friendly `name` so those secrets never reach the JSONL log's `thread_name` field or an error message; the real command is still what executes. Defaults to the joined command, preserving prior behavior.

`ProcessError` gains a `display_name` (and a `display_command` property) used for its rendered message while `.command` keeps the real argv; `FinishedProcess` gains a matching `display_name`.

## 2026-07-01

Added a new async/await ratchet (`test_prevent_async_await`) that freezes the current amount of `async def` / `await` usage in this project and fails if new async code is added. We strongly prefer synchronous code: it is far easier to debug, and our software is intentionally low-scale, so async provides no benefit. Existing usage is grandfathered in at its current count; the count can only decrease.

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
