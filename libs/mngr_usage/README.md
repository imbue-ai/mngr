# imbue-mngr-usage

`mngr usage` -- agent-agnostic CLI for rolling-window usage / quota data.

## What it does

Provides a single `mngr usage` command that surfaces rolling-window usage data
in human / json / jsonl / format-template output. The command itself knows
nothing about any specific agent type or provider; it walks events files on
disk and renders whatever it finds.

## Architecture

This package contains:

- The `mngr usage` CLI command and its rendering helpers.
- The `UsageSnapshot`, `WindowSnapshot`, and `CostSnapshot` data types.

Discovery is by path convention. The CLI walks
`<host_dir>/agents/*/events/<source>/usage/events.jsonl` (the same shape
`mngr transcript` uses for `events/<source>/common_transcript/...`), scans
every event line, and aggregates per `<source>`: rate-limit windows
collapse freshest-wins (the account-level counter), while cost is grouped
per `session_id` and filtered to a recency window (`--since`, default 24h).
The `<source>` segment is free-form -- whatever the writer plugin chose.

When multiple writers contribute, each renders as its own `[source]` section in
human output and as an entry in the JSON `sources` array.

## Destroyed agents

A destroyed agent's usage still counts. Before an agent's (or its whole host's)
state directory is deleted, its `events/<source>/usage` directories are copied to
`<local_host_dir>/preserved/<agent-name>--<agent-id>/`; for remote agents the
files are pulled to the local machine so they survive host destruction. This is
on by default and
controlled by the `preserve_on_destroy` option on the `usage` plugin config (set
it to `false` to discard usage on destroy).

`mngr usage` reads these preserved files back **by default**, so a destroyed
agent's accumulated cost and rate-limit history still counts toward the totals.
Pass `--no-preserved` (on `mngr usage` and `mngr usage wait`) to consider only
live agents.

## Output formats

- `mngr usage` (human summary: per-mode cost line(s) + window lines)
- `mngr usage --detail` (human + per-session breakdown lines)
- `mngr usage --format json` (summary JSON: per-mode aggregates `subscription_cost` and `api_cost`, `session_count` plus per-mode counts, windows)
- `mngr usage --format json --detail` (JSON with `sessions[]` per source; each session carries a `cost_mode` tag)
- `mngr usage --format jsonl`
- `mngr usage --format '5h:{five_hour.used_percentage}%/{seven_day.used_percentage}%'`

The `--detail` flag is independent of `--verbose` (which controls log
level). It toggles only the per-session breakdown surfaces.

## Waiting on a predicate

`mngr usage wait --until <CEL>` blocks until at least one source's CEL
context satisfies every `--until` expression, then exits 0. Composable with
shell:

```
mngr usage wait --until 'five_hour.elapsed_percentage > 75 && five_hour.used_percentage < 50' \
  && mngr message my-agent "ok, kick off the next batch"
```

The CEL context per source mirrors one entry of `mngr usage --format json`'s
`sources` array. Each window exposes the writer-emitted fields
(`used_percentage`, `resets_at`, `window_seconds`, `label`, ...) plus the
reader-derived `seconds_until_reset`, `elapsed_seconds`, and
`elapsed_percentage` (the last two require `window_seconds` from the
writer; absent on variable-duration windows like Claude's overage).
Source-level fields are exposed too. Cost is split by auth mode and
**never lumped**: `subscription_cost.*` aggregates sessions whose Claude
Code process was on a Claude.ai Pro/Max subscription (cost is **imputed**
by Claude Code -- the user actually pays a flat subscription), and
`api_cost.*` aggregates sessions whose process was on a direct
`ANTHROPIC_API_KEY` (cost is **real** billable spend). Pick the mode you
care about, e.g. `api_cost.total_cost_usd > 20.0` means "I've spent more
than $20 of real API money across recent sessions". `session_count` is
the total across both modes; `subscription_session_count` and
`api_session_count` break that down per mode. `since_seconds` and
`sessions[]` (every session in the window, newest-first, each carrying
a `cost_mode` of `"SUBSCRIPTION"` or `"API_KEY"`) are available as well.
Use `--since` to tighten or widen the aggregation window from its
default (24h). For a per-session predicate, index `sessions[]` directly
(e.g. `sessions[0].cost.total_cost_usd > 5` for the most recent
session's own contribution).

Exit codes mirror `mngr wait`: 0 matched, 1 error, 2 timeout. Default poll
interval is 30s; use `--interval` for tighter cadence. To restrict matching
to a specific writer, use the top-level `source` field in CEL (e.g.
`--until 'source == "claude" && five_hour.used_percentage < 50'`).

## Polling from cron (check mode)

For recurring automation, let `cron` own the cadence: poll the plain
`mngr usage --format json` snapshot on a schedule and branch in the shell. See
[cron automation recipes](docs/cron_recipes.md) for worked examples: using up an
about-to-expire 5h window (with a weekly pace check), warming a fresh window the
moment the last one elapses, and working through a queue of task files a couple
at a time.

## Implementing a writer plugin

A writer plugin is responsible for producing `cost_snapshot` events at the
conventional path. The minimal contract is just the JSONL line shape:

```jsonl
{"source":"<your-source>/usage","type":"cost_snapshot","event_id":"evt-<hex>","timestamp":"<ISO 8601>","session_id":"<uuid>","cost":{"total_cost_usd":<float>},"rate_limits":{"<window-key>":{"used_percentage":<float>,"resets_at":<unix-ts>}}}
```

Every event must carry a `session_id` (non-empty string) plus at least one
of `rate_limits` or `cost`. Events missing `session_id` are dropped with a
WARNING log naming the source and event_id -- so if you're writing a usage
plugin and don't see your data in `mngr usage`, check the log first. Any
plausible statusline source has a session identifier handy, so this is
rarely a constraint; if a future tool doesn't, synthesize a stable
per-session id on the writer side. The `type` field is informational --
the reader doesn't validate it -- but `"cost_snapshot"` is the current
convention.

Append one line per refresh to:

```
<agent_state_dir>/events/<your-source>/usage/events.jsonl
```

`mngr usage` will pick it up automatically -- no plugin registration with this
package required.

The writer chooses both the window keys and (optionally) per-window
`label`s. Keys are used by format templates (`{<key>.used_percentage}`) and
should be identifier-safe if you want format-template support; the per-window
`label` controls human display (e.g. `"5h"` vs the literal key `"five_hour"`).
Render order is the writer's insertion order in the JSONL.

Writers may also include `"window_seconds": <int>` per window to declare a
fixed window duration. When present, `mngr usage` derives `elapsed_seconds`
and `elapsed_percentage`, which `mngr usage wait` CEL predicates can use to
express "75% of the window has elapsed" without callers hardcoding window
durations. Omit `window_seconds` for windows without a fixed length (e.g.
Claude's overage indicator).
