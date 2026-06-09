Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the generated
`settings.local.toml` no longer wrote `type = "claude"` twice under `[commands.create]`.
The duplicate key produced invalid TOML ("Cannot overwrite a value"), which made every
e2e tutorial command fail to parse its config. Removing the duplicate restores the intended
single default agent type and unblocks the e2e tutorial tests (e.g. `test_create_headless`).
