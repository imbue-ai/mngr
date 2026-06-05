Added an opt-in, live integration test suite (`imbue/mngr_robinhood/test_sdk_*.py`) that
performs clean-room verification of the documented `query()` and `ClaudeSDKClient` interfaces
of the Claude Agent SDK (`claude_agent_sdk`) end-to-end against the real API. The suite covers
`query()` (string and streaming-input prompts), `ClaudeSDKClient` lifecycle and control
(`connect`/`disconnect`, multi-turn, `receive_messages`/`receive_response`, `set_model`,
`set_permission_mode`, `interrupt`), `can_use_tool` allow/deny and a `PreToolUse` hook,
message/content-block type shapes, the session functions (`list_sessions`,
`get_session_info`, `get_session_messages`, `rename_session`, `tag_session`), and documented
`FileNotFoundError` error paths.

These tests make real, paid API calls and are excluded from all CI runs via the new `sdk_live`
marker; they only run when `RUN_SDK_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are both set (run them
with `just test-sdk-live`). Added `claude-agent-sdk` as a dependency and `pytest-asyncio` as a
dev dependency (`asyncio_mode = "strict"`). See the README's "Running the live SDK tests"
section for details.
