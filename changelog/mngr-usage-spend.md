## mngr usage: per-session cost aggregation across recent sessions

The Claude statusline writer (`mngr_claude_usage`) captures `rate_limits` +
per-render `session_id` + `cost.*` from Claude Code's statusline JSON, into
`events/claude/usage/events.jsonl` (renamed from `events/claude/rate_limits/`
since the file is no longer rate-limit-only). The event `type` is
`cost_snapshot`. The writer no longer skips emission when only `cost` is
present (no `rate_limits`), so cost tracking now works for direct
`ANTHROPIC_API_KEY` users -- Claude Code doesn't emit `rate_limits` for them
(it's Pro/Max only), but `cost` is always present. The writer script is
named `claude_usage_writer.sh` and reads `$MNGR_USAGE_EVENTS_PATH` for
the test override.

`mngr usage` now aggregates cost **per session** within a recency window
instead of just rendering the freshest event's reading:

- Reader scans every line of each agent's events file (not just the last),
  builds a `SessionCostRecord` per `(source, session_id)`, and filters to
  sessions whose last event is within `--since` (default 24h, configurable
  per-invocation or via plugin config).
- Human output: between the source header and the window lines, the
  current session's `session <id>: $X.YY (Xm ago)` line is shown; a
  `total: $A.BB across N sessions in last <since>` line follows when more
  than one session is in the window.
- JSON output: `source.cost` is the aggregate; `source.current_session`
  is the latest session's full record; `source.sessions[]` enumerates all
  sessions in the window; `source.session_count` and `source.since_seconds`
  are also exposed.
- `mngr usage wait --until` CEL surface: `cost.total_cost_usd` is the
  aggregate (sum across recent sessions). To predicate on the current
  session specifically, use `current_session.cost.total_cost_usd`. New
  `--since` flag affects the aggregate.
- Format template: top-level `{cost.total_cost_usd}` is the aggregate;
  `{current_session.session_id}` and `{current_session.cost.total_cost_usd}`
  pick out the latest session.

Examples:

```
mngr usage --since 7d                              # aggregate over 7 days
mngr usage wait --until 'cost.total_cost_usd > 20' # cumulative across recent sessions
mngr usage wait --until 'current_session.cost.total_cost_usd > 5'  # current session only
```
