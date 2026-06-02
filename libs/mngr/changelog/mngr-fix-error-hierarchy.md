`HostError` (and all of its subclasses, e.g. `HostConnectionError`, `HostOfflineError`,
`HostAuthenticationError`, `CommandTimeoutError`, `HostDataSchemaError`) now inherit from
`MngrError` instead of `BaseMngrError`, consolidating the error hierarchy under a single
user-facing parent class. Host errors are now `ClickException` instances, so when one reaches
the CLI it renders as a clean `Error: ...` message (plus any help text) instead of a Python
traceback, and `except MngrError` handlers treat them as the user-facing errors they are.

The base `get_host_and_agent_details` now re-raises `HostConnectionError` from its per-agent
guard so that, even though `HostConnectionError` is now a `MngrError`, a connection failure
still reaches the host-level handler that clears the connection cache and falls back to the
offline view instead of being swallowed per-agent.
