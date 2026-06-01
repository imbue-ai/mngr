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
  (sshd crashed, or PID exhaustion blocked new SSH sessions) -- one of
  the cases `--stop-host` is meant to handle. The host_id is now resolved
  from the discovery event stream and then fetched through the provider's
  own SSH-free `get_host` (which validates that the host still exists and
  supplies its name), so `mngr stop <agent> --stop-host` followed by
  `mngr start <agent>` reliably bounces an unresponsive workspace. A
  single SSH-free lookup against the one relevant provider both validates
  and names the host, so resolution does not scan every provider's hosts
  up front, and does not replay host discovered/destroyed events.
- This supports the minds tiered workspace-restart recovery flow, which
  uses a full host restart as its heavier recovery tier.
- When `--stop-host` targets multiple hosts, they are now stopped
  concurrently (via a concurrency-group executor) instead of one at a
  time, so the command no longer serializes on the slowest host. Output
  order is unchanged. If one host fails to stop, the others are still
  stopped before the error is raised (the previous sequential version
  aborted on the first failure, leaving later hosts running).

Fix `mngr list --format json` crashing on `ProviderErrorInfo` when no
agents were returned. With `--on-error continue` and a per-provider
failure, the empty-agents path passed raw `ErrorInfo` pydantic models to
`json.dumps`, which crashed with `TypeError: Object of type
ProviderErrorInfo is not JSON serializable`. The empty-agents path now
goes through the same `_emit_json_output` serializer as the non-empty
path, so `mngr list --on-error continue --format json` produces a clean
`{"agents": [], "errors": [...]}` payload instead of a traceback.
