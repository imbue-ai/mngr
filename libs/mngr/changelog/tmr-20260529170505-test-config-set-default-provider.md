Fixed the `test_config_set_default_provider` e2e tutorial test. It now verifies
that `mngr config set commands.create.provider modal` persists the value by
reading the project `settings.toml` directly, instead of issuing a follow-up
`mngr config get --scope project`. Writing to the default (project) scope creates
a config file without `is_allowed_in_pytest = true`, which the pytest config guard
rejects on the next `mngr` invocation; inspecting the file on disk avoids that
artifact and is a more faithful verification of the persisted config.
