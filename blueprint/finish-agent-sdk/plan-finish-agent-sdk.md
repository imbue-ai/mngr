# Finish the mngr-backed Agent SDK

Close out the remaining work in `blueprint/mngr-agent-sdk/remaining-work.md` so the
mngr-backed `imbue.mngr_robinhood.agent_sdk` is a faithful drop-in for `claude_agent_sdk`,
keeping the existing "claude-in-tmux + read session JSONL" transport.

## Overview

- Implement six of the eight open gaps on the existing transport (no transport rewrite):
  `fork_session`, computed `total_cost_usd`, `interrupt()`, `can_use_tool` + `hooks`,
  `get_server_info()`, and effective `set_model` / `set_permission_mode`.
- `can_use_tool` + `hooks` (the ~19-test gap) is bridged with a local HTTP server that claude
  settings-file hooks call into; the in-process Python callbacks run there. Chosen over re-hosting
  the SDK's stdio control protocol because mngr already supports claude settings-file hooks and the
  hook JSON contract supports allow/deny/ask plus tool-input rewrite.
- Control-surface actions that mngr can't do live (`interrupt`, `set_model`, `set_permission_mode`)
  are realized by stop + restart of the agent; mngr_claude already auto-resumes the claude session
  on relaunch, so the conversation is preserved for free.
- `fork_session` reuses mngr_claude's existing `--adopt-session` machinery; `total_cost_usd` is
  computed from per-message token usage and a pricing table; `get_server_info()` reads a one-shot
  `claude` stream-json `system/init` probe.
- Two gaps stay permanently real-SDK-only and are documented as such: partial-message streaming
  (`include_partial_messages` -> `StreamEvent`) and the real `system/init` tools list (kept as the
  documented built-in set). Hermeticity also stays as-is (real host settings are required for auth).

## Expected behavior

- `ClaudeSDKClient` with a `can_use_tool` callback gates tools exactly as the real SDK: the callback
  is consulted per non-pre-approved tool call, `allow` lets it run, `allow` + `updated_input`
  rewrites the tool's arguments, `deny` blocks the side effect, and each denial appears in
  `ResultMessage.permission_denials` (with `tool_name`).
- `hooks` callbacks fire in-process: `PreToolUse` / `PostToolUse` (matcher-scoped, multiple matchers
  all fire, non-matching tools skipped) and `UserPromptSubmit` (with the prompt text); a `PreToolUse`
  hook returning `permissionDecision: "deny"` blocks the tool while the run completes as `success`.
- `ClaudeSDKClient.interrupt()` ends an in-flight turn: the response stream terminates at a
  `ResultMessage`, the client stays connected, and a subsequent `query()` continues the same
  conversation. The module-level `query()` is one-shot and does not expose interrupt.
- `set_model` / `set_permission_mode` take effect: the call stops the agent, applies the new value,
  and restarts it (resuming the session) before returning, so the next turn uses the new setting.
- `query()` / `client.query()` with `options.fork_session=True` and a `resume` / `continue_conversation`
  target produces a turn under a brand-new `session_id` (the source session is left intact); calling
  `fork_session=True` with no resume target raises a clear error.
- `ResultMessage.total_cost_usd` is a positive computed value for known models (approximate, from
  token usage x pricing); it is `None` for models absent from the pricing table.
- `get_server_info()` returns a dict containing `commands` and `output_style`; `get_mcp_status()`
  continues to report no MCP servers.
- Behavior unchanged for callers that use none of these features; sessions still only *stop* on
  `disconnect` (remaining readable), but pytest runs no longer leak SDK agents.
- Documented limitations: no `StreamEvent` partial streaming, `system/init` advertises the built-in
  tool set rather than claude's negotiated list, and the agent is not hermetic from host claude config.

## Changes

### `can_use_tool` + `hooks` (HTTP bridge)
- Add a local HTTP bridge that runs inside the SDK process for the connected lifetime of a client /
  query, bound to `127.0.0.1` on an ephemeral port, owned by the session's concurrency group.
- Translate configured `hooks` and (a catch-all `PreToolUse` entry for) `can_use_tool` into claude
  settings-file hook entries written into the agent's claude settings, with matchers mapped from
  `HookMatcher`; the bridge URL is passed to the agent via an environment variable.
- Ship a small hook command script (a wheel-packaged resource) that reads hook stdin, POSTs it to the
  bridge, and prints the bridge's JSON decision to stdout (claude blocks on it synchronously).
- In the bridge, dispatch each request to the right registered Python callback (by event + matcher +
  hook id), then return the documented hook output: for `can_use_tool`, `hookSpecificOutput` with
  `permissionDecision` allow/deny/ask plus `updatedInput`; for hook callbacks, their returned
  `HookJSONOutput`. Construct the `ToolPermissionContext` / `HookContext` passed to callbacks.
- Record denials in the bridge and have the driver fold them into the synthesized
  `ResultMessage.permission_denials` at turn finalization.
- Reconcile with the existing unattended settings so a hook `deny` still blocks even though
  permissions are otherwise auto-allowed (deny always cancels the tool).

### `interrupt()` + restart primitive
- Add a shared in-process "restart with resume" helper built on `host.stop_agents` +
  `host.start_agents` (claude auto-resumes via its launch-command resume guard).
- `ClaudeSDKClient.interrupt()` stops the agent so the in-flight drain ends via the existing
  dead-agent detection and finalizes a `ResultMessage`; the client remains connected and the next
  turn restarts-with-resume the same agent.

### `set_model` / `set_permission_mode`
- Make both eager: write the new value into the agent's claude settings, then stop + restart-with-resume
  inside the call so the running agent reflects the change before the next turn.

### `fork_session`
- On a turn with `fork_session=True`, adopt the source `resume` / `continue_conversation` session into
  a fresh agent via mngr_claude's `--adopt-session` machinery and launch it with `--fork-session`,
  surfacing the new claude `session_id` from the transcript; raise if there is no resume target.

### `total_cost_usd`
- Add a per-model pricing table (input + output + cache-read + cache-write rates) and compute the
  result cost from the accumulated turn `usage`; pass it into the synthesized `ResultMessage`
  (`None` for unknown models).

### `get_server_info()`
- Add a lazy, cached one-shot `claude -p --output-format stream-json` probe (run in the session's
  cwd/config) that parses the `system/init` event for `commands` / `output_style` (and tools), and
  return them from `get_server_info()`. `system/init` emitted during turns keeps the built-in tool list.

### Tests / infra
- Drop the `requires_native_sdk` skip from the now-passing mngr-target tests (permissions, hooks,
  interrupt, sessions-fork, cost, server-info) and keep it only for partial-message streaming.
- Add offline unit tests for the new pure logic (cost computation, hook/option -> settings translation,
  bridge request/response shaping, `system/init` probe parsing, fork/adopt argument building).
- Add an opt-in reaper / idle-timeout so SDK agents don't accumulate, and ensure pytest teardown
  destroys only `robinhood-`labelled SDK agents in the test cwd (never other agents).
- Update `libs/mngr_robinhood/README.md` for the dual-target suite and the new capabilities, run the
  full both-target `just test-sdk-live` end-to-end, and report the results.
- Add the per-project changelog entry (`libs/mngr_robinhood/changelog/<branch>.md`).

### Out of scope (deferred, stated explicitly)
- Partial-message streaming (`include_partial_messages` -> `StreamEvent`).
- Real negotiated `system/init` tools list; full agent hermeticity from host claude config.
- Remote-host SDK sessions and non-claude agent types.
