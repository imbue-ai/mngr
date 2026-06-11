Documented the cross-plugin `waiting_reason` parity picture: the agent-plugin-parity spec now records that antigravity and opencode emit no permission-request event (so only `END_OF_TURN` is feasible for them), while codex can support both `PERMISSIONS` and `END_OF_TURN`.

Added a codex `waiting_reason` scoping note (`specs/agent-plugin-parity/codex-waiting-reason-scoping.md`) detailing the hooks, marker scripts, async-subagent concurrency analysis, and the open live-firing verification for codex's `PermissionRequest` hook.
