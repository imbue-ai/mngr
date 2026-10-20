The pi lifecycle extension now writes its common transcript as the ATIF-shaped
stream (`specs/atif-transcript-alignment/spec.md`) instead of the old
`user_message`/`assistant_message`/`tool_result` envelope.

The stream opens with a `header` line pinning `ATIF-v1.7`, written once when the
file is created (a resumed restart appends to the existing stream). Each pi
assistant message becomes one `step` record with ATIF field names, user messages
become `user` steps, and `toolResult` messages become `observation` records keyed
to their call by `source_call_id`.

Full fidelity: tool arguments and tool output are no longer truncated at 200 /
2000 characters -- `mngr transcript` truncates for display instead. Thinking
blocks are captured as `reasoning_content` (previously dropped entirely), and
images render as the `[image omitted]` text placeholder.

Token usage now uses the ATIF metric names: `prompt_tokens` counts all input
including cache reads and cache writes, `cached_tokens` counts the cache reads,
the cache-write count rides under `metrics.extra.cache_creation_input_tokens`, and
pi's client-side per-message cost fills ATIF's `cost_usd`.

Compaction summaries, previously dropped, become `system` steps carrying ATIF
v1.7's `context_management` mark with the summary as an inline observation.

An empty or whitespace-only native tool argument payload is now emitted as an
empty `arguments` object rather than `{"_raw": ""}`, matching the codex and
antigravity converters -- an absent payload means "no arguments".
