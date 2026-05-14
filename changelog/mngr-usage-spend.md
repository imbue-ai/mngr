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
instead of just rendering the freshest event's reading, and keeps
**subscription** and **API-key** spend in separate aggregates so imputed
estimates never get lumped with real billable spend:

- Reader scans every line of each agent's events file (not just the last),
  partitions each agent's events into Claude Code processes via cost-drop
  detection (cost is process-cumulative; `/clear` doesn't reset it), and
  within each process builds a `SessionCostRecord` per session whose `cost`
  is its delta from the prior session's cumulative reading.
- Each session is tagged with a `cost_mode`: `SUBSCRIPTION` if any event in
  its Claude Code process carried `rate_limits` (Claude.ai Pro/Max --
  cost is imputed by Claude Code, the user actually pays a flat subscription)
  or `API_KEY` otherwise (direct `ANTHROPIC_API_KEY` -- cost is real
  billable spend).
- Sessions are filtered to those whose last event is within `--since`
  (default 24h, configurable per-invocation or via plugin config).
- Human output (default): one cost line per mode that contributed --
  `subscription cost (imputed): $X.YY ...` and/or `api cost: $X.YY ...`
  -- followed by the populated rate-limit window lines. Subscription is
  rendered first; either or both can be present.
- Human output with `--detail`: adds indented per-session lines (newest-first)
  between the cost lines and the window lines, each tagged `[sub]` or `[api]`.
- JSON output (default): `source.subscription_cost.*` and `source.api_cost.*`
  are the per-mode aggregates; `source.subscription_session_count`,
  `source.api_session_count`, and `source.session_count` (total) are also
  exposed. There is intentionally **no** combined `source.cost` field.
  `sessions[]` is omitted unless `--detail` is set.
- JSON output with `--detail`: adds `source.sessions[]` (newest-first
  records, each carrying `cost_mode`).
- `mngr usage wait --until` CEL surface: `subscription_cost.total_cost_usd`
  and `api_cost.total_cost_usd` are the per-mode aggregates; no combined
  `cost` field exists. To predicate on a specific session, index
  `sessions[]` directly. New `--since` flag affects the aggregates.
- Format template: top-level `{subscription_cost.*}` / `{api_cost.*}` keys;
  the format-template surface intentionally doesn't expose per-session
  paths (use `--format json` if you need them).

Examples:

```
mngr usage --since 7d                                # aggregate over 7 days
mngr usage wait --until 'api_cost.total_cost_usd > 20'  # real billable spend crossed $20
mngr usage wait --until 'subscription_cost.total_cost_usd > 50'  # imputed >$50 of value
mngr usage wait --until 'sessions[0].cost.total_cost_usd > 5'  # most recent session only
```
