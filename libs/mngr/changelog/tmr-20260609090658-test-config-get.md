Fixed the e2e test fixture's local `settings.local.toml` template, which had a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate made the
file invalid TOML, so any test that exercised `mngr config set --scope local`
(e.g. the `test_config_get` tutorial test) failed when tomlkit re-parsed the file
with "Cannot overwrite a value".
