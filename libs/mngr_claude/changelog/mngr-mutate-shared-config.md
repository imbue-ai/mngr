Shared Claude config mode (`isolate_local_config_dir = false`) now dismisses the
cosmetic startup dialogs (trust, onboarding, effort callout, cost threshold)
directly in your default Claude config so they no longer intercept automated
input. Previously shared mode left the config untouched, so a fresh `~/.claude.json`
re-triggered the trust/onboarding screens on every agent.

mngr writes these dismissals into the file claude actually reads
(`$CLAUDE_CONFIG_DIR/.claude.json`, or `~/.claude.json` when the var is unset), and
honors `auto_dismiss_dialogs` in this mode. It never accepts bypass-permissions
mode via the global config -- that remains governed by `settings.json` -- and still
does no per-agent settings.json or keychain provisioning.

Also fixed: in shared mode, mngr's hooks are now installed when shared mode is set
via the current `isolate_local_config_dir = false` flag (previously they were only
installed when set via the deprecated `use_env_config_dir = true` alias).
