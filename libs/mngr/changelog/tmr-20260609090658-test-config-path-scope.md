Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`, which made every config
command fail to parse the file ("Cannot overwrite a value"). This unblocks
`test_config_path_scope` and the other CONFIGURATION tutorial e2e tests.
