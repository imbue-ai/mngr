Fixed the e2e tutorial test harness so that `mngr config set` against the
project scope works under pytest. The shared e2e fixture now seeds the
project `settings.toml` with the `is_allowed_in_pytest` opt-in (previously
only `settings.local.toml` opted in, so a project-scope `config set` created
a file that the pytest config guard rejected on the next load), and opts the
harness into the assign-by-default merge semantics
(`allow_settings_key_assignment_narrowing = true`) so a project-scope
`commands.create.*` setting no longer collides with the local-scope
`connect_command` in the command's shared `defaults` map. This unblocks the
`test_config_set_default_provider` and `test_config_set_headless` tutorial
tests.
