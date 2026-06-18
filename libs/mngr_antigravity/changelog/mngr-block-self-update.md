Added an `update_policy` field to the antigravity agent type that governs agy's background self-updater. `NEVER` sets `AGY_CLI_DISABLE_AUTO_UPDATE=true` in the agent environment so the installed build stays put; `AUTO` leaves agy's self-updater enabled; `ASK` behaves like `AUTO`. When unset, it resolves to `NEVER` for unattended (remote/deploy) agents and `AUTO` for attended local agents.

Note: agy has no version-pinning capability -- Google's installer always installs the latest build -- so there is no `version` field. Use `update_policy = "NEVER"` to freeze whatever build was installed.
