Relocate Gemini CLI's entire config dir under each agent's state dir via `GEMINI_CLI_HOME`, replacing the previous system-tier-settings approach. Mirrors how `mngr_claude` uses `CLAUDE_CONFIG_DIR` for full per-agent isolation: each agent now gets its own `.gemini/settings.json`, `.gemini/trustedFolders.json`, `.gemini/history/`, `.gemini/tmp/`, and `.gemini/installation_id` under `$MNGR_AGENT_STATE_DIR/plugin/gemini/.gemini/` instead of sharing `~/.gemini/`. The user's `~/.gemini/` is never written to.

`GeminiAgent.provision()` now seeds the relocated dir with three pieces:

1. **`settings.json`** — merged from the user's `~/.gemini/settings.json` (so `security.auth.selectedType` is preserved and the agent inherits the user's OAuth setup) plus mngr's hook builders layered on top via `merge_hooks_config`.
2. **`trustedFolders.json`** — explicit `TRUST_FOLDER` entry for `work_dir`, replacing the previous `GEMINI_CLI_TRUST_WORKSPACE=true` env var.
3. **Auth artifact symlinks** — `oauth_creds.json`, `google_accounts.json`, `installation_id` symlinked from `~/.gemini/` so the agent inherits the user's existing Google login without copying tokens. Missing artifacts are skipped silently (a user who hasn't run gemini interactively yet won't have all three).

`modify_env_vars()` is now a single env var: `GEMINI_CLI_HOME`. The previous `GEMINI_CLI_SYSTEM_SETTINGS_PATH` and `GEMINI_CLI_TRUST_WORKSPACE` env vars are gone.

On remote hosts, `_symlink_user_auth_artifacts` issues the `ln -sf` via `host.execute_idempotent_command`, so the symlink is created server-side; the source paths it points at must already exist under `~/.gemini/` on the remote machine (or the agent should use API-key auth instead). A future PR mirroring `mngr_claude._provision_local_credentials` may add a copy-and-keep-in-sync mode for cross-machine setups.
