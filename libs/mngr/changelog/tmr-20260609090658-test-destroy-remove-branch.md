Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` and caused every e2e
tutorial test to fail with a TOML parse error during `mngr create`.

Added `test_destroy_keeps_branch_by_default`, a companion test for the
`mngr destroy --remove-created-branch` tutorial block that verifies the documented
safe default: a plain destroy leaves the agent's git branch intact.
