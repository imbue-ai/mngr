# Plan: mngr-backed Claude Agent SDK (`imbue.mngr_robinhood.agent_sdk`)

## Refined prompt

> we want to create an alternative implementation for the API / SDK interface that was tested in the mngr/claude-sdk-tests branch (ie, functions like query(), list_session(), etc, and the ClaudeSDKClient class)
>
> The core idea is that we can simply use mngr in order to implement this implementation with *any* agent that is supported by mngr. This can be done by using the mngr API (ie, what gets invoked via the the mngr CLI, but calling from in-process in order to keep it more direct, much like we already do to implement a "claude -p" like interface in the robinhood plugin)
>
> We'll want to make our own interfaces in imbue.mngr_robinhood.agent_sdk so that we can do something like: from imbue.mngr_robinhood.agent_sdk import query, ClaudeAgentOptions
> * Re-export / subclass the real `claude_agent_sdk` message, block, and option types so shapes match exactly (keep `claude-agent-sdk` as a runtime dependency).
> * v1 targets the **claude agent type only**; keep the seams clean so other mngr agent types can be added later.
>
> We ought to re-use much of the work from orchestrator.py. We may need to refactor a little bit of it to be more re-usable.
> * Populate the rich response fields (cost, usage, model id, tool-use/result blocks) by parsing the agent's native per-session JSONL transcript directly, exactly as the robinhood orchestrator already does; non-claude agent types get best-effort placeholders.
> * Extract a shared, reusable turn-driver / transcript-reader module from `orchestrator.py` that both the robinhood CLI `run()` and the new `agent_sdk` build on.
> * Add a parser that converts the native raw stream-json transcript directly into `claude_agent_sdk` dataclasses.
>
> There are clearly certain bits of functionality that we won't be able to capture (ex: we can stream messages as they are completed, but we won't be able to stream individual words, because mngr transcript and mngr events don't necessarily expose that granularity for agent responses)
> * Start with a small hand-written core test set for the new implementation and grow toward full conformance; build out the control surface (`can_use_tool`/`hooks`/`interrupt`/partial-streaming) incrementally.
>
> We'll want to start with the core functionality for the ClaudeSDKClient class (being able to send and receive messages, list the various sessions, etc). Sessions should be mngr agents run via the robinhood mechanism (eg, prefixed with that, available in mngr list, etc)
> * Use claude's native session UUID as the SDK `session_id` (derived from the claude mngr agent), map it to the mngr agent, and implement `rename_session`→mngr rename and `tag_session`→mngr label.
> * `disconnect()` / end of a `query()` **stops** the mngr agent but leaves its session readable; reaping is done by mngr `cleanup`/`gc` (tests destroy via a fixture for hygiene).
> * `resume` / `continue_conversation` reuse the same mngr agent/session id (mngr already resumes it); `fork_session` raises `NotImplementedError` for now.
> * v1 core ships both `query()` and `ClaudeSDKClient` (lifecycle + multi-turn) together; the first deliverable must pass: query string-prompt, client lifecycle/multi-turn, session read functions, session mutators, and message/block type-shape assertions.
> * `agent_sdk` constructs its own `MngrContext` from the user's mngr config (zero-config import, like the CLI).
> * Bridge the async SDK surface to mngr's synchronous in-process API by wrapping blocking calls in `asyncio.to_thread`; keep the existing sync turn-driver.
>
> We can defer filling in the more complex and detailed bits until after we've got the basics working, but we *do* want to consider them as part of the design here (the whole point of this spec is to get a core approach that will be able to eventually pass most of the tests)
> * Parameterize every test via an `sdk` fixture that yields the implementation module, so each test runs against **both** the real `claude_agent_sdk` and `imbue.mngr_robinhood.agent_sdk`.
> * "Exact equivalence" means each target independently passes the **same documented-contract assertions** (types, field presence, behavioral invariants), not byte-identical values.
> * Rewrite the existing `test_sdk_*.py` files in place to use the `sdk` fixture (one suite, both targets).

---

## Overview

- Build `imbue.mngr_robinhood.agent_sdk`: a drop-in re-implementation of the Claude Agent SDK Python surface (`query`, `ClaudeSDKClient`, `ClaudeAgentOptions`, the message/block types, and the session functions) backed by mngr agents instead of a direct `claude` subprocess.
- Reuse the robinhood machinery: a single mngr claude agent per session, driven through the in-process mngr API (`api_create`, `send_message_to_agents`, events/transcript reading), with the turn-driver and transcript reader factored out of `orchestrator.py` into a shared module.
- Sessions are first-class mngr agents (`robinhood-`-prefixed, visible in `mngr list`); the SDK `session_id` is claude's native session UUID derived from the agent, and `rename`/`tag` map onto mngr rename/labels.
- Achieve type-shape parity by re-exporting/subclassing the real `claude_agent_sdk` dataclasses and populating their rich fields (model, usage, cost, tool blocks) from a new parser over the native stream-json transcript that robinhood already tails.
- v1 is claude-only and covers the core read/write/lifecycle surface; the advanced control surface (`can_use_tool`, `hooks`, `interrupt`, `fork_session`, partial-message streaming) is designed-for but stubbed (raising a clear not-implemented error) and grown incrementally toward full conformance.

## Expected behavior

- `from imbue.mngr_robinhood.agent_sdk import query, ClaudeSDKClient, ClaudeAgentOptions, ...` exposes the same public names as `claude_agent_sdk`; importing requires no caller-supplied mngr context.
- `query(prompt, options)` runs one turn on a fresh mngr claude agent and async-yields `SystemMessage` (init) → `AssistantMessage`(s) (and `UserMessage` tool results) → exactly one terminal `ResultMessage`, matching the documented stream contract.
- `ClaudeSDKClient` supports the documented lifecycle: async context manager, explicit `connect()`/`disconnect()`, `query()`, `receive_response()` (terminates at the `ResultMessage`), `receive_messages()` (lower-level, caller breaks), and multiple turns on one connection with a stable `session_id`.
- Each turn's messages carry real values parsed from the agent's transcript: `AssistantMessage.model` resolves to the selected model, `ResultMessage` carries `session_id`/`is_error`/`subtype`/`num_turns`/`duration_ms`/`usage`/`total_cost_usd`, and tool turns produce correlated `ToolUseBlock`/`ToolResultBlock`.
- Ending a turn/connection **stops** (does not destroy) the mngr agent; the session remains discoverable and readable until reaped by mngr `cleanup`/`gc`.
- Session functions operate by directory: `list_sessions(directory=...)` enumerates `robinhood-` agents for that cwd (newest-first, with `limit`/`offset`), and `get_session_info` / `get_session_messages` read a single session back; unknown ids return `None` / `[]` as documented.
- `rename_session` / `tag_session` persist via mngr (rename → agent rename, tag → agent label) and are visible on the next `get_session_info` / `list_sessions`; clearing a tag restores `None`; mutating an unknown id raises `FileNotFoundError`.
- `resume=<session_id>` and `continue_conversation=True` continue an existing session (same `session_id`); `fork_session=True` raises `NotImplementedError` for now.
- The control surface present in the type signatures but not yet wired (`can_use_tool`, `hooks`, `interrupt`, `set_model`/`set_permission_mode` beyond no-op-safe cases, `include_partial_messages`/`StreamEvent`) is accepted by the API and raises a clear, explicit not-implemented error rather than silently misbehaving.
- The existing `test_sdk_*.py` suite, rewritten to use an `sdk` fixture, runs every test against both the real SDK and `agent_sdk`; both targets must satisfy the same documented-contract assertions. Tests for not-yet-implemented surfaces are skipped/xfail for the `agent_sdk` target only.

## Changes

- **New subpackage `imbue.mngr_robinhood.agent_sdk`** that exports the documented public names (`query`, `ClaudeSDKClient`, `ClaudeAgentOptions`, the message/content-block/result/system types, the session functions, and the permission/hook types) so it is import-compatible with `claude_agent_sdk`.
- **Type parity layer** that re-exports or subclasses the real `claude_agent_sdk` dataclasses, so `isinstance` checks and field shapes match exactly without redefining them.
- **Refactor `orchestrator.py`** to extract a reusable, context-driven turn-driver and transcript-reader (agent create → deliver prompt → wait-for-turn-end → drain transcript), leaving the robinhood CLI `run()` as a thin caller of the shared module.
- **New transcript→SDK parser** that converts the native raw stream-json transcript (the source robinhood already tails) into the `claude_agent_sdk` message/block dataclasses with their rich fields populated.
- **Session model** mapping claude's native session UUID to a `robinhood-`-prefixed mngr agent: derive the session id for a given claude agent, enumerate/read sessions via the mngr list/transcript APIs keyed by cwd, and implement `rename`/`tag` on top of mngr rename + labels.
- **Lifecycle change**: SDK turns stop (not destroy) their agent so sessions stay readable; reaping is delegated to mngr `cleanup`/`gc`, and tests clean up via a fixture.
- **Async bridge** so the async SDK methods drive the synchronous mngr API/turn-driver via a worker thread, without rewriting the driver to be natively async.
- **Zero-config context bootstrap** so the SDK builds its own `MngrContext` from the user's mngr config on first use.
- **Stubs for the deferred control surface** (`can_use_tool`, `hooks`, `interrupt`, `fork_session`, partial-message streaming) that exist in the signatures and raise an explicit not-implemented error, with the intended wiring documented for later phases.
- **Rewrite the existing `test_sdk_*.py` suite** to bind names through an `sdk` fixture and run against both targets, with per-surface skips/xfail for `agent_sdk` features not yet implemented; keep them under the opt-in `sdk_live` marker.
- **Packaging/docs**: ensure `mngr_robinhood` depends on what `agent_sdk` needs (mngr, mngr_claude, `claude-agent-sdk`), add the `agent_sdk` import-linter layer, a `mngr_robinhood/changelog/<branch>.md` entry, and a README section describing the new in-process SDK.

## Notes / open items to confirm during implementation

- How exactly to derive a claude agent's native session UUID from a `robinhood-` mngr agent (transcript `system/init` `session_id`, the per-session JSONL filename, or agent state) — the chosen source must be stable across the agent's lifetime.
- Whether stopped agents remain in `mngr list` long enough (and with transcripts intact) for the session read path; if not, fall back to reading persisted transcript files directly.
- Idle/cost control for kept-alive agents (e.g. whether to set a conservative `--idle-timeout` at create time even though reaping is via `cleanup`/`gc`).
- Fidelity caveats for fields mngr cannot always observe; these are placeholders for non-claude agents (out of v1 scope) but should be real for claude.

✓ Explore  ✓ Plan  ● Write  ○ Refine
