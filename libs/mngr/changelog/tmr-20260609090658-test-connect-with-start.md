Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml`, which caused every e2e tutorial test to fail with a TOML
"Cannot overwrite a value" parse error when running `mngr create`.

Added `test_connect_with_start_restarts_stopped_agent`, an e2e test that shares
the `mngr connect --start` tutorial block but stops the agent first, verifying
that `--start` actually restarts a stopped agent (the existing test only connects
to an already-running agent, where `--start` is a no-op).
