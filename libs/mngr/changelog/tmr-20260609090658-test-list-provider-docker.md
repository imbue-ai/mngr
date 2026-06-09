Fixed a bug in the shared e2e test fixture (`e2e/conftest.py`) where the generated
`settings.local.toml` contained a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate key made the file invalid TOML, so every `mngr`
command run by an e2e test failed during config parsing with
`Cannot overwrite a value`. Removing the redundant line restores a valid config and
unblocks the docker tutorial e2e tests (and all other e2e tests sharing this fixture).
