Fixed the e2e test fixture that seeded an invalid `settings.local.toml`: a bad
merge had added a duplicate `type = "claude"` key under `[commands.create]`,
which made the file unparseable TOML and broke `test_config_set_scope` (and any
other config test that loads the local layer).
