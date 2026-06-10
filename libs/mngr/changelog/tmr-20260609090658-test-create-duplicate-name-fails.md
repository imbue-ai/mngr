Fixed the e2e test fixture so `mngr` commands run again: the generated
`settings.local.toml` had a duplicate `type = "claude"` key under
`[commands.create]`, which made the config fail to parse with "Cannot overwrite
a value" and broke every e2e test. Also gave `test_create_duplicate_name_fails`
a 120s timeout (matching sibling e2e tests) since it creates a live agent and
runs `mngr list`, whose provider discovery can exceed the default 10s timeout.
