# mngr-backed Claude Agent SDK (`imbue.mngr_robinhood.agent_sdk`)

Started an alternative, mngr-backed implementation of the Claude Agent SDK Python interface,
importable as a drop-in replacement for `claude_agent_sdk`:

```python
from imbue.mngr_robinhood.agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient
```

The new `imbue.mngr_robinhood.agent_sdk` module re-exports every SDK *type* (options, messages,
content blocks, session info, permission/hook types) verbatim from `claude_agent_sdk` so that
`isinstance` checks and field shapes are identical, and re-implements the behavioral entry points
(`query`, `ClaudeSDKClient`, and the session functions) on top of mngr: each session is a
`robinhood-`-prefixed mngr claude agent.

This first phase lands the verified foundation:

- A pure parser that converts claude's native per-session JSONL transcript into the documented
  `claude_agent_sdk` message/content-block dataclasses (text/thinking/tool-use/tool-result
  blocks, assistant/user messages), plus helpers that synthesize the `system`/`init` and terminal
  `result` messages.
- The async `ClaudeSDKClient` lifecycle/streaming surface (`connect`/`disconnect`/`query`/
  `receive_response`/`receive_messages`, async context manager, streaming-input prompt coercion)
  and the one-shot `query()` wrapper.
- A zero-config `MngrContext` builder so the SDK can be imported and called without any mngr
  plumbing.

The live agent-driving seam (creating the mngr agent, draining its transcript per turn) and the
session functions are scaffolded with the precise mngr wiring documented; they currently raise a
clear not-implemented error and are built out and verified against a real claude agent in the next
phase. v1 targets the claude agent type only.
