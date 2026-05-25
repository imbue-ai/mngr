# Delete the dead imbue_cloud inject helpers

`build_combined_inject_command` and `normalize_inject_args` (and the
`_sed_replace_env_line` / `_ensure_no_quote_chars` helpers that only
they called) were added to support a "claim CLI" pattern that never
landed. Trimming the `minds_api_key` argument earlier in this branch
left them with no caller anywhere in the monorepo except their own
test file; the central `MINDS_API_KEY` is now injected by the
latchkey gateway's `minds-api-proxy` extension on the fly, not
pushed down onto a leased pool host.

This change deletes those four functions and the entire `host_test.py`
file. The live `provision_agent` path on `ImbueCloudHost` still uses
`_build_patch_claude_config_command`, which stays.
