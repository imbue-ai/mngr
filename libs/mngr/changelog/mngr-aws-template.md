Fixed environment-variable forwarding for remote streaming SSH commands
(`OuterHost.execute_streaming_command` with `env=...`). The streaming path
prepended env vars as a bare `KEY=VAL command` prefix, which in the shell only
applies to the single simple command it precedes -- so for a compound command
like `install && tool ...` the var was gone by the time the second command ran.
It now uses `export KEY=VAL && command` (mirroring the non-streaming pyinfra
path), so the var is set in the shell environment for the whole command. This is
what made remote `depot build` fail with "missing API token" even though
`DEPOT_TOKEN` was supplied via `env`. Extracted the prefixing into a pure
`_prepend_env_exports` helper with unit tests.
