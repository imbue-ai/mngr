Fixed the e2e test fixture's generated `settings.local.toml`, which had a duplicate `type = "claude"` key under `[commands.create]`. The duplicate produced invalid TOML ("Cannot overwrite a value"), causing every e2e command to fail to parse its config. Removed the duplicate line so the fixture writes valid TOML again.

Strengthened `test_create_command_custom_script` to confirm the forwarded command is actually running as a process inside the agent (via `mngr exec ... ps`), rather than only checking the recorded metadata and state.
