Added an opt-in, live integration test suite (`imbue/mngr_robinhood/test_sdk_*.py`) that
performs clean-room verification of the documented `query()` and `ClaudeSDKClient` interfaces
of the Claude Agent SDK (`claude_agent_sdk`) end-to-end against the real API. The suite covers
`query()` (string and streaming-input prompts), `ClaudeSDKClient` lifecycle and control
(`connect`/`disconnect`, multi-turn, `receive_messages`/`receive_response`, `set_model`,
`set_permission_mode`, `interrupt`), `can_use_tool` allow/deny and a `PreToolUse` hook,
message/content-block type shapes, the session functions (`list_sessions`,
`get_session_info`, `get_session_messages`, `rename_session`, `tag_session`), and documented
`FileNotFoundError` error paths.

The suite was then expanded with ~100 additional tests covering: field-level message contracts
(`SystemMessage` init data, `ResultMessage` usage/cost/duration/model-usage, `AssistantMessage`
metadata, `StreamEvent`); built-in tool use end-to-end (Bash/Read/Write/Edit/Glob/Grep, tool
use/result id correlation, failing-command `is_error`); more `ClaudeAgentOptions` behavior
(`env`, `allowed_tools`/`disallowed_tools`, `permission_mode` bypass/accept/plan, pinned
`model`, `system_prompt` string and preset, `add_dirs`, `cwd`); `ClaudeSDKClient` introspection
(`get_server_info`, `get_mcp_status`) and streaming input / partial messages; advanced
permission and hook behavior (`can_use_tool` input rewriting and deny semantics, `PreToolUse`/
`PostToolUse`/`UserPromptSubmit` hooks and matchers); and session continuation (`resume`,
`fork_session`, `continue_conversation`) plus `SDKSessionInfo`/`SessionMessage` field contracts
and `list_sessions` paging.

A dedicated `test_sdk_session_functions.py` adds thorough coverage of the five session
functions (`list_sessions`, `get_session_messages`, `get_session_info`, `rename_session`,
`tag_session`): `limit`/`offset` paging on real sessions, `list_sessions` newest-first
ordering, `SDKSessionInfo` field-value contracts (summary/tag/custom_title/file_size/created_at),
directory isolation, and rename/tag overwrite-and-clear semantics.

These tests make real, paid API calls and are excluded from all CI runs via the new `sdk_live`
marker; they only run when `RUN_SDK_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are both set (run them
with `just test-sdk-live`). Added `claude-agent-sdk` as a dependency and `pytest-asyncio` as a
dev dependency (`asyncio_mode = "strict"`). See the README's "Running the live SDK tests"
section for details.
