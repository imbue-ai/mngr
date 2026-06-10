Fixed the e2e tutorial test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that
wrote a duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. The duplicate key caused every e2e tutorial command to fail
with a TOML parse error ("Cannot overwrite a value"). Removing the redundant line
restores config parsing so the e2e tutorial tests run.
