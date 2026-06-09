Fixed the e2e tutorial test fixture (`e2e` in `conftest.py`) which wrote a malformed `settings.local.toml` containing a duplicate `type = "claude"` key under `[commands.create]`. The config loader now rejects duplicate TOML keys, so this broke every e2e tutorial test with "Cannot overwrite a value". Removed the accidental duplicate line.

Also strengthened `test_recipe_launch_check_cleanup` to verify that destroy removes the agent itself (gone from `mngr list`, no longer resolvable by `mngr exec`), not just its branch.
