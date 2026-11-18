`background_tasks` replaces `leases` on `AgentDetails`, in `mngr list --format json`, `--fields`, CEL filters and `mngr observe` events. Each item has `name`, `kind` (`SHELL`, `SUBAGENT`, `MONITOR`, `SCHEDULE` or `OTHER`), `native_type` (the harness's own type name), `started_at` and `last_seen_at`; `expires_at` is gone. A row that still carries `leases` is accepted and the key dropped.

Background task records move from an agent's `leases/` directory to `background_tasks/` and no longer expire: an agent reads `RUNNING_BACKGROUND` exactly when it has a record and no open turn. `HOOK_WORK_LEASE_TTL_SECONDS` now only bounds how long a record keeps the agent's activity clock fresh.

An agent created on an earlier version keeps writing under `leases/`, which mngr no longer reads: it never reads `RUNNING_BACKGROUND` and publishes no background tasks, while its own hooks and heartbeat keep working among themselves. Imbue Studio's busy checks use the workspace template's task markers and are unaffected.

The `--running` help describes its states as a turn in flight or background tasks recorded.
