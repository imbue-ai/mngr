Fixed the e2e tutorial test fixture so that the generated `settings.local.toml`
no longer contains a duplicate `type = "claude"` key under `[commands.create]`.
The duplicate produced invalid TOML that tomlkit rejected when `mngr config set
... --scope local` re-saved the file, breaking config tutorial tests such as
`test_config_list_json`.
