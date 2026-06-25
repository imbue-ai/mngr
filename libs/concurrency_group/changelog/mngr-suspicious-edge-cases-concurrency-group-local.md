Hardened several suspicious edge-case handlers in the concurrency_group library:

- `ShutdownEvent.wait` now reports the event's true state when it cannot acquire the internal busy-wait lock within the timeout, instead of always returning `False` (which could tell a caller "not shutting down" while a shutdown was already in progress).
- `run_local_command_modern_version` only labels a process as timed-out when it was actually killed for exceeding its deadline; a process that exits on its own near the deadline boundary is no longer mislabeled (which previously could surface a spurious `ProcessTimeoutError`).
- The subprocess initialization-success callback is no longer inside the broad failure-reporting `try`, removing a latent double-notification path.
- Replaced an unreachable `ProcessSetupError` fallback in `RunningProcess.wait` with an assertion, and added clarifying comments to a few intentional defensive branches.
