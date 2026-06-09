Fixed the e2e test fixture, which wrote a duplicate `type = "claude"` key under
`[commands.create]` in the generated `settings.local.toml`, causing every e2e
test to fail with a TOML parse error ("Cannot overwrite a value"). The default
agent type is now written exactly once.

Strengthened `test_destroy_multiple_at_once` to also assert on the
"Successfully destroyed 3 agent(s)" summary line, verifying that a single
`mngr destroy a b c --force` command tears down the exact number of agents
requested.
