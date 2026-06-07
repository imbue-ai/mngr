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
  resumed session (previously a no-op); this can switch to a genuinely different model mid-session.
- `get_server_info()` returns real commands / output style from a one-shot `claude` stream-json probe.
- `ResultMessage.total_cost_usd` is computed from per-turn token usage and a per-model price table.
- `include_partial_messages` now yields `StreamEvent`s on the mngr target: the agent's tmux pane is
  watched (via mngr_claude's streaming `stream_buffer`) and the reconstructed assistant text is
  wrapped in the claude-native partial-event sequence (`message_start` -> `content_block_delta` ->
  `message_stop`). The event shapes conform to the real SDK; the text is approximate (reconstructed
  from the rendered pane, not claude's token-level deltas) and `usage`/`total_cost_usd` stay on the
  authoritative transcript-derived `ResultMessage`. Off by default; the caller's model is honored
  (the SDK does not force sonnet). The `stream_buffer` parse/diff logic is shared with the robinhood
  CLI streaming path via a new `stream_buffer.py` module.

The corresponding live tests are now unskipped for the mngr target (they previously ran only
against the real SDK). The README documents the dual-target live suite and the supported control
surfaces.

`fork_session` remains real-SDK-only: claude's `--fork-session` does not assign a new session id
when driven interactively over an adopted, resumed session, so the mngr-backed SDK raises
`AgentSdkNotImplementedError` rather than returning a wrong/duplicate id.
