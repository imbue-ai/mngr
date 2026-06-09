Fixed the e2e test fixture's `settings.local.toml` template, which contained a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate made
the file invalid TOML, so every `mngr` command in the docker tutorial e2e tests
failed with "Cannot overwrite a value" instead of exercising the docker provider.
