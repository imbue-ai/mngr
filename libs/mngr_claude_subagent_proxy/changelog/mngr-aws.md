## parse_int_env moved to imbue_common

The private `parse_int_env` helper in `mngr_claude_subagent_proxy.hook_io` was lifted into a new `imbue_common.env_vars` module so the same int-valued env-var parsing pattern is centralized for use across the monorepo. Local call sites (`hooks/deny.py`, `hooks/spawn.py`) updated to import from there. No behavior change.
