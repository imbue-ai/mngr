New `mngr stop --stop-host` flag: stops an agent's whole host (every
agent on it) instead of just the named agent.

- For container-backed providers `--stop-host` stops the container while
  the underlying machine keeps running; it is rejected up front on
  providers that do not support stopping hosts, and cannot be combined
  with `--archive`.
- `--stop-host` is idempotent: if the host is already offline it reports
  success instead of raising an error, so restarting an already-stopped
  workspace works.
- `--stop-host` now resolves the target host without SSH. Previously it
  failed with an `SSH error (Error reading SSH protocol banner...)` when
  the host's container was still running but its sshd was unreachable
  (sshd crashed, or PID exhaustion blocked new SSH sessions) -- exactly
  the situation `--stop-host` exists to recover from. The host is now
  resolved from the discovery event stream, cross-checked against an
  SSH-free host scan, so `mngr stop <agent> --stop-host` followed by
  `mngr start <agent>` reliably bounces an unresponsive workspace.
- This supports the minds tiered workspace-restart recovery flow, which
  uses a full host restart as its heavier recovery tier.
