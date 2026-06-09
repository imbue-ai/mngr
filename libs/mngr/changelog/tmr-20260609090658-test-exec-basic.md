Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an
invalid `settings.local.toml`: the `[commands.create]` table defined `type = "claude"`
twice, which TOML rejects ("Cannot overwrite a value"). This caused every `mngr create`
in the e2e tutorial suite to fail at setup with a config-parse error. Removed the
duplicate key so the fixture writes a single `type = "claude"` default, unblocking the
`mngr exec` tutorial tests (and all other e2e tests sharing this fixture).
