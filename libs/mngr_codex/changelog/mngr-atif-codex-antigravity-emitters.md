The codex common-transcript emitter now writes ATIF-shaped stream records
(`header` / `step` / `observation`) instead of the legacy
`user_message` / `assistant_message` / `tool_result` envelopes, per
`specs/atif-transcript-alignment/spec.md`. Every stream opens with a
`header` line pinning `ATIF-v1.7`, and the envelope's `source` field is
renamed `emitter` (ATIF claims `source` for the step originator).

Full fidelity: tool `arguments` is the complete parsed JSON object (a
`custom_tool_call` script or any other non-object invocation rides whole under
`_raw`) and tool output is recorded untruncated. The 200-character input
previews and 2000-character output cap are gone; truncation is now purely a
display-time concern in `mngr transcript`.

Tool calls carry codex's native `call_id` as the ATIF `tool_call_id`, so
results pair back to their call without a synthetic id. An output whose call
was never seen is no longer dropped -- it is emitted with the tool name
`unknown` so the output is never lost.

Newly captured: rollout `reasoning` items become agent steps carrying
`reasoning_content` (dropped only when the item exposes nothing but its
encrypted payload), and codex's instruction injections -- the AGENTS.md context
blob and the `<user_instructions>` / `<environment_context>` initial-context
items -- become system steps carrying their full text instead of being silently
dropped.

An assistant message carrying no text no longer produces an empty agent step,
matching how an empty user message is dropped. codex records a tool invocation
as its own rollout item, so such a message has nothing left to show.
