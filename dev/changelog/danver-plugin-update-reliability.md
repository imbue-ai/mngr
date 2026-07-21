Make the SessionStart plugin-update hook (`scripts/claude_update_plugin.sh`) reliable:

- Stop wiping the plugin cache before updating, so a failed update (offline, git auth) no longer strips every plugin skill (`/autofix`, `/verify-conversation`, ...) from the session.

- Surface update and install failures instead of silently swallowing them; the final warning goes to the hook's stdout, which Claude Code injects into the session context, so the agent itself can explain why a mandated skill is missing. A short ssh connect timeout keeps all attempts within the hook time budget even when fully offline.

- Fall back to `claude plugin install` when an update fails because the plugin was never installed for the current scope (a fresh machine, or a new Sculptor workspace path); the install lands at user scope, so future workspaces inherit it.
