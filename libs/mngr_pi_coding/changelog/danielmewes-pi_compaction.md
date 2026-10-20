Add support for context compaction in `pi-coding` agents (`HasCompactionMixin`):
- Implement `request_compaction`, `get_cache_ttl_minutes`, `get_context_tokens`, and `get_idle_since` on `PiCodingAgent`.
- Provide compaction helper utilities for tracking compaction state, idle state detection, cache TTL resolution (Anthropic 60m, OpenAI 30m), and token extraction from transcripts.
- Handle compaction requests via the inbox sentinel (`mngr_compact`) in `mngr_pi_lifecycle.ts` by invoking `ctx.compact()`.
- Listen for `session_compact` events in `mngr_pi_lifecycle.ts` and emit raw compaction records, ATIF v1.7 common transcript compaction steps, and usage cost snapshots.
