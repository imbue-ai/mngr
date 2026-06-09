Fixed the e2e test fixture so the shared `settings.local.toml` it writes is valid
TOML. The `[commands.create]` table contained a duplicate `type = "claude"` key,
which `tomlkit` rejects with "Cannot overwrite a value". This broke any e2e command
that loaded the merged config (e.g. `mngr config edit`), surfacing a config-parse
error instead of the command's real behavior.

Also strengthened `test_config_edit_editor_failure` to assert that `mngr config edit`
propagates the editor's exact exit code (1 from `/bin/false`) rather than merely
exiting non-zero.
