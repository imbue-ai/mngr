Fixed `mngr plugin list` mislabeling opt-in plugins (e.g. `claude_subagent_proxy`) as `enabled=true` when they are actually blocked.

The reported `enabled` state now reflects the plugin's real block state: opt-in plugins that were not explicitly enabled show `enabled=false`, while a plugin enabled via `[plugins.<name>] enabled = true` still shows `enabled=true`.

Underlying this, `config.disabled_plugins` now faithfully includes opt-in plugins that are disabled by default, so every consumer of that field (not just the plugin list) sees the correct effective disabled set. This is suppressed under `MNGR_LOAD_ALL_PLUGINS`, matching plugin-manager startup, so doc/tooling runs still load every plugin.
