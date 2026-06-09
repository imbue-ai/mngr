Fixed the e2e test fixture that seeds the local-scope config file: it wrote a
duplicate `type = "claude"` key under `[commands.create]`, producing an
unparseable `settings.local.toml`. This caused `test_config_path_invalid_scope`
(and any command that loaded the merged config) to fail with a TOML parse error
instead of exercising the intended behavior.
