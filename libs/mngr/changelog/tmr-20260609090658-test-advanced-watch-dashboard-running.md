Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) so the
`settings.local.toml` it writes is valid TOML. The fixture had an accidental
duplicate `type = "claude"` key under `[commands.create]`, which made the
strict (tomllib) config read path fail with "Cannot overwrite a value". This
broke e2e tests whose commands parse config strictly, e.g.
`mngr list --running --format json` in `test_advanced_watch_dashboard_running`.
