# Reuse the shared assistant-text extractor

`subagent_wait.extract_assistant_text` now delegates to the shared
`imbue.mngr_claude.stream_json.assistant_text` typed boundary rather than duplicating its own
content-block scan. Behavior is unchanged (it still returns the concatenation of the assistant
message's text blocks, or the empty string), but the envelope-parsing logic now lives in one place.
