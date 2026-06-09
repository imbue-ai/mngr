Fixed the e2e test fixture (`conftest.py`) that wrote an invalid `settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`, which made every e2e `mngr create` fail with a TOML "Cannot overwrite a value" parse error.

Strengthened `test_multiple_agents_coexist` to verify that coexisting agents each occupy a distinct working directory (their own worktree), rather than only checking that an `echo` command runs on each.
