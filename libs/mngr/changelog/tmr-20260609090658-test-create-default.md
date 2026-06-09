Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) that wrote a
`settings.local.toml` containing a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate produced invalid TOML, causing every e2e
tutorial test that loads the local config to fail with "Cannot overwrite a
value". Removed the duplicate line.
