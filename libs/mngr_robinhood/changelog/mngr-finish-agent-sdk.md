# Finish the mngr-backed Agent SDK

Completed the previously-stubbed control surfaces of the mngr-backed Agent SDK
(`imbue.mngr_robinhood.agent_sdk`) so it is a faithful drop-in for `claude_agent_sdk`:

- `can_use_tool` and `hooks` callbacks now fire in-process, served by a local HTTP bridge that
  the mngr claude agent calls via `--settings` hook commands (PreToolUse/PostToolUse/
  UserPromptSubmit). Permission allow/deny/`updated_input` all work, and denials are surfaced in
  `ResultMessage.permission_denials`.
- `ClaudeSDKClient.interrupt()` now ends an in-flight turn (the response stream is streamed
  incrementally and terminates at a `ResultMessage`); the next `query()` continues the conversation.
- `set_model` / `set_permission_mode` now take effect by rewriting the agent's stored launch
  command with the new configuration (via the agent's `set_command` API) and restarting it on the
  resumed session (previously a no-op).
- `fork_session` is implemented via mngr_claude's session-adoption machinery (new session id).
- `get_server_info()` returns real commands / output style from a one-shot `claude` stream-json probe.
- `ResultMessage.total_cost_usd` is computed from per-turn token usage and a per-model price table.

The corresponding live tests are now unskipped for the mngr target (they previously ran only
against the real SDK). The README documents the dual-target live suite and the supported control
surfaces. Partial-message `StreamEvent` streaming remains real-SDK-only by design.
