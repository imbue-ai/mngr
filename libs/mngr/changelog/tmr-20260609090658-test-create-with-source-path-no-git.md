Fixed the e2e tutorial test fixture, which wrote a duplicate `type = "claude"` key into `[commands.create]` in the generated `settings.local.toml`. The duplicate key made the file invalid TOML, causing every e2e tutorial command to fail with a config parse error ("Cannot overwrite a value").

Also strengthened `test_create_with_source_path_no_git` to assert that a non-git source folder produces no agent git branch and that the agent's work directory is not a git repository, reinforcing the tutorial's claim that mngr does not require git.
