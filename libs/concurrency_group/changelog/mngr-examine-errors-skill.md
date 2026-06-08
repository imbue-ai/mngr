Fixed several over-defensive edge-case handlers that could silently mask failures (surfaced by the new `identify-suspicious-edge-cases` skill):

- `poll()` no longer fabricates a magic `1007` exit code for a run-thread that finished with neither a result nor an exception; it now raises `ProcessInvariantError` instead of letting a fake status flow downstream.
- A submitted task that raises `BaseException` (e.g. `KeyboardInterrupt`) now completes its future via `set_exception` instead of leaving `result()` hung forever.
- `_shutdown_popen` raises `ProcessTerminationError` when a process cannot be killed, rather than returning a clean-looking `None` return code.
- Group teardown no longer swallows thread-join exceptions with a blanket `except Exception: pass`, and a dominant `BaseException` during exit no longer discards sibling `Exception` failures (they are aggregated into a `ConcurrencyExceptionGroup`).
- Force-kill on timeout now uses a real grace period and reports accurately when a process could not be killed (the message previously always claimed success).
- `ShutdownEvent()` constructed bare is now always safe to call `is_set()` on.

New library error types `ProcessInvariantError` and `ProcessTerminationError`.

Follow-up: fixed a memory-visibility race in `RunningProcess.poll()` exposed by the stricter invariant above. When the run thread had just finished, `poll()` read `_completed_process` without synchronizing against the thread's write, so under load it could observe a stale `None` and raise a spurious `ProcessInvariantError`. `poll()` now `join()`s the finished thread first, establishing a happens-before edge that makes the result visible (and surfacing any captured exception), before deciding the invariant was violated. The invariant still fires loudly for a genuinely resultless, exceptionless thread.