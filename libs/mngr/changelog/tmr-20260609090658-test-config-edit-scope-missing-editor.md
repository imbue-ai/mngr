Fixed the e2e tutorial test fixture which wrote a duplicate `type = "claude"`
key under `[commands.create]` in `settings.local.toml`. The duplicate produced
invalid TOML, causing `mngr` commands in affected e2e tests to fail at config
parse time with "Cannot overwrite a value" instead of exercising the command
under test. Also hardened `test_config_edit_scope_missing_editor` to assert the
missing-editor error names the editor and points at `$EDITOR`/`$VISUAL`.
