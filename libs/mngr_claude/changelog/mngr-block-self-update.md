Added an `update_policy` field to the claude agent type that governs Claude Code's background auto-updater. `NEVER` sets `DISABLE_AUTOUPDATER=1` in the agent environment so the installed (optionally `version`-pinned) binary stays put; `AUTO` leaves the auto-updater enabled; `ASK` behaves like `AUTO` (claude has no interactive update flow). When unset, it defaults to `NEVER`.

**Behavior change:** claude agents now disable Claude Code's auto-updater by default (local and remote). Previously mngr did not disable it on local agents -- the per-agent config inherited your `~/.claude.json` `autoUpdates` value, so local agents typically auto-updated. Set `update_policy = "AUTO"` to opt back into the auto-updater. The policy is ignored in `use_env_config_dir` (shared) mode, where mngr leaves your claude environment alone.

Pin a specific version with `version` to control exactly what gets installed.
