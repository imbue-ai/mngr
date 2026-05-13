## mngr usage: capture session cost alongside rate limits

The Claude statusline writer (`mngr_claude_usage`) now also captures per-render
`session_id` and `cost.*` (total_cost_usd, total_duration_ms, ...) from Claude
Code's statusline JSON, piggybacking on the existing event file at
`events/claude/rate_limits/events.jsonl`. The event `type` field is now
`cost_snapshot` (previously `rate_limit_snapshot`).

Practical effect: cost tracking now works for direct-`ANTHROPIC_API_KEY` users
too -- they never receive `rate_limits` in the statusline payload (per Claude
Code docs, that's Pro/Max only), but `cost` is always present. The writer now
emits an event whenever either `rate_limits` or `cost` is present, instead of
only when `rate_limits` is present.

In `mngr usage`:

- Human output now shows a `session <id>: $<amount>` line between the source
  header and the window lines (session ID truncated to 8 chars; full UUID in
  JSON / format-template surfaces).
- JSON output adds `session_id` and `cost` keys to each source entry.
- `mngr usage wait --until` CEL predicates can use new fields:
  `cost.total_cost_usd`, `cost.total_duration_ms`, `session_id`. E.g.
  `mngr usage wait --until 'cost.total_cost_usd > 5.0'`.
- Format templates expose `{session_id}` and `{cost.total_cost_usd}` etc.
