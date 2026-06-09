Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`), which wrote a
`settings.local.toml` with a duplicate `type = "claude"` key under `[commands.create]`.
TOML rejects the duplicate ("Cannot overwrite a value"), which made every e2e command
(`mngr create`, etc.) fail to parse its config. Removed the redundant line.

Added `@pytest.mark.timeout(300)` to the `test_rename.py` e2e tests so that
`mngr list --format json` has time to complete remote-provider discovery (matching the
convention used by the other create+list e2e tests), and strengthened
`test_create_and_rename_agent` to verify the renamed agent is preserved in place (same
command, still alive) rather than only checking its name.
