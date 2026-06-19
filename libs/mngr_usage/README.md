# imbue-mngr-usage

`mngr usage` -- agent-agnostic CLI for rolling-window usage / quota and cost data.

`mngr usage` surfaces usage data for your agents in human / json / jsonl /
format-template output. It aggregates per source: rate-limit windows collapse
freshest-wins (an account-level counter), while cost is grouped per session and
filtered to a recency window. When multiple agent types contribute, each renders
as its own `[source]` section in human output and as an entry in the JSON
`sources` array.

## Destroyed agents

A destroyed agent's usage still counts. Before an agent (or its host) is
deleted, its usage data is preserved locally (for remote agents the files are
pulled to the local machine). This is on by default; set `preserve_on_destroy`
to `false` on the `usage` plugin config to discard usage on destroy.

`mngr usage` reads these preserved files back by default. Pass `--no-preserved`
(on `mngr usage` and `mngr usage wait`) to consider only live agents.

## Filtering by event age

Pass `--since DURATION` (e.g. `--since 1h`, `--since 7d`) on `mngr usage` or
`mngr usage wait` to restrict the per-session cost aggregation by age. Sessions
whose last event is older than `--since` are dropped from `sessions[]` and from
the per-mode aggregates. Default is 24h, configurable via the `since_seconds`
option on the `usage` plugin config.

`--since` only shapes the cost surface. Rate-limit windows always reflect the
freshest reading across all agents.

`--stale-after` is not an age filter: it only controls whether the human output
prints a "snapshot last updated X ago" warning.

## Output formats

- `mngr usage` (human summary: per-mode cost line(s) + window lines)
- `mngr usage --detail` (human + per-session breakdown lines)
- `mngr usage --format json` (summary JSON: per-mode aggregates `subscription_cost` and `api_cost`, `session_count` plus per-mode counts, windows)
- `mngr usage --format json --detail` (JSON with `sessions[]` per source; each session carries a `cost_mode` tag)
- `mngr usage --format jsonl`
- `mngr usage --format '5h:{five_hour.used_percentage}%/{seven_day.used_percentage}%'`

`--detail` is independent of `--verbose` (which controls log level); it toggles
only the per-session breakdown surfaces.

## Waiting on a predicate

`mngr usage wait --until <CEL>` blocks until at least one source's CEL context
satisfies every `--until` expression, then exits 0. Composable with shell:

```
mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50' \
  && mngr message my-agent "ok, kick off the next batch"
```

The CEL context per source mirrors one entry of `mngr usage --format json`'s
`sources` array. Each window exposes fields like `used_percentage`, `resets_at`,
`window_seconds`, `label`, plus derived `seconds_until_reset`, `elapsed_seconds`,
and `elapsed_percentage` (the last two are absent on variable-duration windows
like Claude's overage). Cost is split by auth mode and never lumped:
`subscription_cost.*` aggregates sessions on a Claude.ai Pro/Max subscription
(cost is imputed by Claude Code -- the user pays a flat subscription), and
`api_cost.*` aggregates sessions on a direct API key (real billable spend). For
example, `api_cost.total_cost_usd > 20.0` means "I've spent more than $20 of real
API money across recent sessions". `session_count` is the total across both
modes; `subscription_session_count` and `api_session_count` break it down. For a
per-session predicate, index `sessions[]` directly (e.g.
`sessions[0].cost.total_cost_usd > 5`).

Exit codes mirror `mngr wait`: 0 matched, 1 error, 2 timeout. Default poll
interval is 30s; use `--interval` for tighter cadence. To restrict matching to a
specific source, use the top-level `source` field in CEL (e.g.
`--until 'source == "claude" && five_hour.used_percentage < 50'`).

## Polling from cron (check mode)

For recurring automation, let `cron` own the cadence: poll the plain
`mngr usage --format json` snapshot on a schedule and branch in the shell. See
[cron automation recipes](https://github.com/imbue-ai/mngr/blob/main/libs/mngr_usage/imbue/mngr_usage/docs/cron_recipes.md) for worked examples.
