Fixed the e2e test fixture's generated `settings.local.toml`, which contained a
duplicate `type = "claude"` key under `[commands.create]` (a squash-merge
artifact) that produced invalid TOML and broke every e2e test with a config
parse error. Also strengthened `test_plugin_list_active_to_see_types` to verify,
via JSON output, that the `claude`, `codex`, and `command` agent types appear as
their own enabled plugin entries rather than relying on loose substring matches.
