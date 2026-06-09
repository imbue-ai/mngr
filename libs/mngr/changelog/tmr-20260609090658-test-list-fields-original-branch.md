Fixed the e2e test fixture so it no longer emits an invalid `settings.local.toml`: the
`[commands.create]` table set `type = "claude"` twice, which is a duplicate-key TOML error
("Cannot overwrite a value") that caused every command loading the merged config (e.g.
`mngr list`) to fail. Removed the duplicate key.

Added a happy-path companion to the `mngr list --fields "name,state,initial_branch"` tutorial
test (`test_list_fields_original_branch_with_agent`) that creates an agent and asserts the
`initial_branch` column actually displays the branch mngr created for it (`mngr/my-task`),
complementing the existing empty-list ("No agents found") coverage.
