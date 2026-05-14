# Gemini agents now produce a common transcript readable by `mngr transcript`

`mngr transcript <gemini-agent>` now works the same way it does for Claude: a background
process polls gemini's session JSONL files and converts user messages, assistant messages,
tool calls, and tool results into the agent-agnostic format at
`events/gemini/common_transcript/events.jsonl`. Multiple gemini agents on the same host
produce disjoint transcripts because sessions are filtered by `.project_root`.

Set `emit_common_transcript = false` on a gemini agent type to opt out.

Internally, a new `HasCommonTranscriptMixin` on `AgentInterface` formalises the contract
that any agent emits its events in this format. `ClaudeAgent` and `GeminiAgent` both
satisfy it; future agent types get `mngr transcript` support for free by implementing
`get_common_transcript_scripts` and shipping a per-agent converter.
