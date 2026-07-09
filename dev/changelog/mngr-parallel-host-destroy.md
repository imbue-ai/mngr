- Changed: the `destroy-pool-host` justfile recipe is renamed to `destroy-pool-hosts` and now takes any number of pool-host ids (clean break, no alias). It forwards to `minds pool destroy`, which destroys all named slices in parallel after atomically claiming each row so a user lease cannot race the destroy. The `minds-justfile` skill doc was updated to match.

- Changed: the pre-commit `regenerate-cli-docs` hook now also triggers on plugin CLI files (`libs/mngr_*/imbue/**/cli/*.py`), so editing a plugin's click commands can no longer leave the generated `libs/mngr/docs/commands/` reference stale until an unrelated PR trips the check.

- Added: `just list-servers` and `just prep-server <server-id>` recipes wrapping the new env-aware `minds server {list,prep}` commands (DSN + pool SSH key resolved from the activated tier automatically). The `minds-justfile` skill doc was updated to match.
