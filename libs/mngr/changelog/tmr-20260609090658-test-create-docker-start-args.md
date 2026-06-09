Fixed a duplicate `type = "claude"` key in the e2e test fixture's generated
`settings.local.toml` (`libs/mngr/imbue/mngr/e2e/conftest.py`). The duplicate
produced a "Cannot overwrite a value" TOML parse error that caused every e2e
tutorial `mngr` command to exit 1 before reaching the provider. This unblocks
the Docker create tutorial tests (and all other e2e tests sharing the fixture).
