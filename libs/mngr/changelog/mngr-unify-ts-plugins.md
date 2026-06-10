Added a canonical schema for the agent-agnostic common-transcript envelope
(`imbue.mngr.agents.common_transcript_records`). It is the single source of truth
for the `user_message` / `assistant_message` / `tool_result` records every agent
plugin emits into the stream `mngr transcript` reads, with a validator and a
conformance test asserting that all five emitters -- claude, antigravity,
opencode, pi-coding, and codex -- produce records matching it, so the
independently written emitters cannot silently drift on the shared fields.

Added a shared agent release-lifecycle harness
(`imbue.mngr.utils.agent_release_testing`) that drives the common create -> WAITING ->
message -> transcript -> stop/start resume -> destroy arc with per-agent profiles, so
each plugin's release test is a thin profile and every agent is held to the same
lifecycle and the same canonical-transcript contract.
