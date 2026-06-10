Fixed the e2e test fixture so `mngr config` tutorial tests work again.

- The shared e2e fixture wrote `settings.local.toml` with the `type = "claude"` key duplicated
  under `[commands.create]`, producing invalid TOML. Any `mngr config` command that loaded the
  merged config (e.g. `mngr config list --scope user`) then failed with a "Cannot overwrite a
  value" parse error. Removed the duplicate so the fixture emits valid TOML.
- Strengthened `test_config_list_scope` to assert real scope isolation: the fixture's
  `connect_command` value lives only in the local scope, so it must appear under
  `--scope local` and must not bleed into the user/project views.
