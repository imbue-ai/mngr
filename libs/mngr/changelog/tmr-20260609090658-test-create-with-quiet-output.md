Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
duplicate `type = "claude"` key under `[commands.create]` in the generated
`settings.local.toml`. TOML rejects the duplicate key, causing every e2e test
that loads this config (including `test_create_with_quiet_output`) to fail with
"Cannot overwrite a value". Removed the duplicate line.
