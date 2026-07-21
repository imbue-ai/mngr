Make the SessionStart plugin-update hook (`scripts/claude_update_plugin.sh`) reliable:

- Stop wiping the plugin cache before updating, so a failed update (offline, git auth) no longer strips every plugin skill (`/autofix`, `/verify-conversation`, ...) from the session.

- Surface update and install failures as warnings instead of silently swallowing them.

- Fall back to `claude plugin install` when an update fails because the plugin was never installed for the current scope (a fresh machine, or a new Sculptor workspace path); the install lands at user scope, so future workspaces inherit it.
