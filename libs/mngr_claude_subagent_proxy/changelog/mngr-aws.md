## AWS provider support: parse_int_env moved to imbue_common

The private `parse_int_env` helper in `mngr_claude_subagent_proxy.hook_io` was lifted into `imbue_common.env_vars` so it can be shared with the new `mngr_aws` provider. Local call sites (`hooks/deny.py`, `hooks/spawn.py`) updated to import from there. No behavior change.
