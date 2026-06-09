Fixed the e2e test fixture that seeded a malformed `settings.local.toml` with a
duplicate `type = "claude"` key under `[commands.create]`. The duplicate caused
`mngr config set` (which round-trips the file through tomlkit) to fail with
"Cannot overwrite a value", breaking `test_config_set` and other config tests.
