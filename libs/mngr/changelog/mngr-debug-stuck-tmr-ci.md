Make `Host.stop_agents`' `timeout_seconds` parameter actually bound wall-clock time, so an unreachable host can't wedge the cleanup path.

Before this change, `timeout_seconds` was only used as the SIGTERM→SIGKILL grace period inside the remote kill command (capped at 1s). The SSH calls around it had no timeout. When the host had silently gone away (e.g. a Modal sandbox torn down before the harness polled it), individual SSH calls would block on the kernel's TCP retransmit timeout — observed at ~16 minutes per call. In TMR's serial polling loop this dropped the steady-state finalize rate to one agent per ~16 minutes, which left most tests pending when the 4h GHA cap fired.

Changes:
- `stop_agents` now computes a `deadline = monotonic() + timeout_seconds` and passes the remaining budget to every internal `execute_idempotent_command` call, so paramiko's channel timeout actually fires.
- Replace the in-loop `_get_agent_by_id` lookup (which routes through SFTP `get_file` — no exposed timeout) with `_read_agent_name_via_shell`, which reads `data.json` via `cat` and therefore honours the deadline.
- Thread `deadline` through `_collect_session_pids`, `_collect_pids_by_agent_id_env`, and `_get_all_descendant_pids` (private helpers; signatures use a keyword-only `deadline=None` so other callers are unaffected).
- Catch `(OSError, HostConnectionError)` at the `stop_agents` boundary and log a warning instead of propagating, matching the existing `get_state` pattern. Callers stop seeing spurious exceptions when the remote host has died mid-cleanup.
- Add two unit tests: one verifies that connection errors during `stop_agents` are swallowed; the other verifies that every SSH call receives a positive `_timeout`.
