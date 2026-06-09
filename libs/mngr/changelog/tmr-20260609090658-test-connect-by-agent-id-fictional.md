Fixed the e2e tutorial test fixture, which wrote an invalid `settings.local.toml` with a
duplicated `type = "claude"` key under `[commands.create]`. This caused every command run
through the fixture to abort with a TOML parse error ("cannot overwrite a value") instead of
exercising the actual code path.

Also strengthened `test_connect_by_agent_id_fictional` to assert that connecting to a
nonexistent agent id reports a clean user-facing "not found" error with no leaked Python
traceback.
