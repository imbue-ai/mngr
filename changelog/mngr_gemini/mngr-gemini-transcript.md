# Gemini agents now produce a common transcript readable by `mngr transcript`

`mngr transcript <gemini-agent>` now works the same way it does for Claude: a background
process polls gemini's session JSONL files and converts user messages, assistant messages,
tool calls, and tool results into the agent-agnostic format at
`events/gemini/common_transcript/events.jsonl`. Multiple gemini agents on the same host
produce disjoint transcripts because sessions are filtered by `.project_root`.

Set `emit_common_transcript = false` on a gemini agent type to opt out.

The gemini plugin also captures the *raw* gemini session JSONL verbatim into
`logs/gemini_transcript/events.jsonl`. This preserves every field gemini emits (model
metadata, internal blocks, etc.) and lives inside the agent state dir, so the transcript
survives cleanup of gemini's own `~/.gemini/tmp/` working directories.

`GeminiAgent` satisfies the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins
by implementing `get_raw_transcript_scripts` + `get_common_transcript_scripts` and
shipping the matching per-agent scripts.
