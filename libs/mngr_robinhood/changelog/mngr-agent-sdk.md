# mngr-backed Claude Agent SDK (`imbue.mngr_robinhood.agent_sdk`)

Added an alternative, mngr-backed implementation of the Claude Agent SDK Python interface,
importable as a drop-in replacement for `claude_agent_sdk`:

```python
from imbue.mngr_robinhood.agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient
```

The new `imbue.mngr_robinhood.agent_sdk` module re-exports every SDK *type* verbatim from
`claude_agent_sdk` (so `isinstance` checks and field shapes are identical) and re-implements the
behavioral entry points on top of mngr: each session is a `robinhood-`-prefixed mngr claude agent,
driven through the in-process mngr API and read back from its native transcript. This works with
any agent mngr can run; v1 targets the claude agent type.

Implemented and verified live against the real API:

- `query()` (string and streaming-input prompts) and the full `ClaudeSDKClient` lifecycle
  (async context manager, `connect`/`disconnect`, `query`, `receive_response`/`receive_messages`,
  multi-turn on one connection with a stable `session_id`).
- The observable `ClaudeAgentOptions` subset: `model`, `system_prompt` (string + preset/append),
  `allowed_tools`/`disallowed_tools`, `permission_mode` (default/acceptEdits/plan/bypass),
  `cwd`, `add_dirs`, `env`, `settings`, `max_turns`.
- Built-in tool use end-to-end (Bash/Read/Write/Edit/Glob/Grep), with correlated
  `ToolUseBlock`/`ToolResultBlock`.
- Message/content-block/result type shapes, including a synthesized `system`/`init` message and a
  terminal `ResultMessage` (with `usage`, `model_usage`, durations, `session_id`, `uuid`).
- The session functions keyed by `directory`: `list_sessions` (newest-first, `limit`/`offset`),
  `get_session_info`, `get_session_messages`, `rename_session`, `tag_session`, with the documented
  `None`/`[]`/`FileNotFoundError` contracts. `session_id` is read from the transcript (never
  assumed equal to the mngr agent id, since claude rotates session ids).
- `resume` / `continue_conversation` by reusing and restarting the agent that owns the session.

Known limitations on the mngr transport (the tests skip the mngr target for these and still run
them against the real SDK): in-process `can_use_tool` / `hooks` callbacks, `interrupt`,
partial-message `StreamEvent` streaming, live `get_server_info`, `fork_session`, and
`total_cost_usd` (absent from claude's native session JSONL). `set_model` / `set_permission_mode`
are accepted but do not retroactively re-configure the already-running agent.

Test suite: the existing `test_sdk_*.py` live suite is parameterized over an `sdk` fixture so each
test runs against both the real `claude_agent_sdk` and the mngr implementation.

Also extracted shared agent-runtime helpers out of `orchestrator.py` into `agent_runtime.py`
(used by both the robinhood CLI and the SDK), and hardened env forwarding to drop shell-unsafe
values that could corrupt the agent's env file.
