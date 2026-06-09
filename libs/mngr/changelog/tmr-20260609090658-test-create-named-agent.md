Fixed the e2e test fixture that generated an invalid `settings.local.toml`: the
`[commands.create]` table set `type = "claude"` twice, which is not valid TOML
("Cannot overwrite a value") and caused every e2e tutorial test to fail at config
load. Removed the duplicate key.

Also added an e2e test (`test_create_unnamed_agent_gets_random_name`) covering the
documented behavior that `mngr create` with no name argument generates a random
agent name.
