Hardened suspicious edge-case handling in `imbue/mngr/hosts/` (a cleanup pass). Internal robustness/clarity changes with no intended user-visible behavior change:

- Offline host state derivation now documents that `stop_reason` carries a `HostState` name (including `DESTROYED`), and the stale `stop_reason` field docstring was corrected.
- `Host.get_state`, `OuterHost._close_paramiko_client`, `OuterHost.write_file` (remote), and the remote `_get_file_mtime` now log the errors they catch instead of swallowing them silently.
- `_get_agent_additional_commands` now raises a clear `CorruptedAgentDataError` on a malformed `additional_commands` entry instead of an opaque `KeyError`/`TypeError`.
- `validate_and_create_discovered_agent` now skips records whose `id`/`name` are the wrong type (not just invalid strings) consistently, instead of crashing discovery.
- Tightened a too-broad `except` in uptime parsing and removed a double-fallback when merging agent labels.
- Added clarifying comments documenting why several edge-case handlers are intentional (cooperative-lock cleanup, SSH transport lookup, exception classification by message text, tmux lifecycle parsing, git base-branch default).
