The opencode plugin now writes its common transcript as the ATIF-shaped stream
(`specs/atif-transcript-alignment/spec.md`) instead of the old
`user_message`/`assistant_message`/`tool_result` envelope.

Every rebuild opens with a `header` line pinning `ATIF-v1.7`. Each assistant
message becomes one `step` record with ATIF field names (`message`, `model_name`,
`tool_calls` with `function_name` + a complete `arguments` object), user messages
become `user` steps, and tool results become `observation` records keyed to their
call by `source_call_id`.

Full fidelity: tool arguments and tool output are no longer truncated at 200 /
2000 characters -- `mngr transcript` truncates for display instead. Reasoning
parts are captured as `reasoning_content`, and image parts render as the
`[image omitted]` text placeholder.

Per-agent annotations moved under the ATIF `extra` objects: a step carries
`conversation_id`, `message_id`, and `finish_reason` under its own `extra`, and
each observation result carries `conversation_id`, `message_id`, `is_error`, and
`tool_name` under the result's `extra`. The stream's `source` framing field is
renamed `emitter` (ATIF claims `source` for the step originator).

An empty or whitespace-only native tool input is now emitted as an empty
`arguments` object rather than `{"_raw": ""}`, matching the codex and antigravity
converters -- an absent payload means "no arguments".
