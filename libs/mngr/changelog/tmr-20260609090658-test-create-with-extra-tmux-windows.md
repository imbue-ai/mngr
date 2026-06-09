Fixed the e2e tutorial test fixture: `settings.local.toml` was written with a duplicate
`type = "claude"` key under `[commands.create]`, which made every `mngr` command in the e2e
tutorial suite fail to parse its config ("Cannot overwrite a value"). Removed the duplicate key.

Also strengthened `test_create_with_extra_tmux_windows` to verify that the extra tmux windows
are actually running their configured commands (not just that windows with the right names
exist), matching the tutorial's promise that `-w name="cmd"` starts a window running that command.
