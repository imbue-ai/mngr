Fixed the e2e tutorial fixture so `mngr create` works again: the generated
`settings.local.toml` had a duplicate `type = "claude"` key under
`[commands.create]`, which made the TOML parser reject the config ("Cannot
overwrite a value"). Removed the duplicate.

Gave `test_exec_git_log` a `@pytest.mark.timeout(60)` override (matching the
identically-shaped `test_exec_branch_show_current`), since `mngr create` plus
`mngr exec` together exceed the 10s default pytest timeout.
