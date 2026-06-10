Fixed the e2e test fixture (`libs/mngr/imbue/mngr/e2e/conftest.py`) which wrote a
`settings.local.toml` containing a duplicate `type = "claude"` key under
`[commands.create]`. The duplicate (a merge artifact) made the file invalid TOML,
so every `mngr create` invoked from an e2e test aborted with
`Failed to parse config file ...: Cannot overwrite a value`. Removing the duplicate
key restores the fixture so the docker (and all other) tutorial e2e tests can run.
