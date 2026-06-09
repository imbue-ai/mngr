Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote an
invalid `settings.local.toml` with a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate key caused every e2e test using this fixture
to fail with "Cannot overwrite a value" when mngr parsed the config, before any
command logic ran. Removed the duplicate line.
