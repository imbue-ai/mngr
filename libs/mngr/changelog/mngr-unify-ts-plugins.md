Added a canonical schema for the agent-agnostic common-transcript envelope
(`imbue.mngr.agents.common_transcript_records`). It is the single source of truth
for the `user_message` / `assistant_message` / `tool_result` records every agent
plugin emits into the stream `mngr transcript` reads, with a validator and a
conformance test asserting that all five emitters -- claude, antigravity,
opencode, pi-coding, and codex -- produce records matching it, so the
independently written emitters cannot silently drift on the shared fields.
