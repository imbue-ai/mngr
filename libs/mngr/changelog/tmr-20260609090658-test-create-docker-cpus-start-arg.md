Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. The duplicate key produced invalid TOML, so every e2e
command that loaded the merged config failed with `Failed to parse config file
... Cannot overwrite a value`. Removed the redundant line so the fixture emits
valid TOML.
