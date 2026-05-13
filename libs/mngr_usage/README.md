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
- The `UsageSnapshot` and `WindowSnapshot` data types.

Discovery is by path convention. The CLI walks
`<host_dir>/agents/*/events/<source>/rate_limits/events.jsonl` (the same shape
`mngr transcript` uses for `events/<source>/common_transcript/...`), reads the
last event from each file, and renders the freshest snapshot per `<source>`.
The `<source>` segment is free-form -- whatever the writer plugin chose.

When multiple writers contribute, each renders as its own `[source]` section in
human output and as an entry in the JSON `sources` array.

## Output formats

- `mngr usage` (human, with stale warning when applicable)
- `mngr usage --format json`
- `mngr usage --format jsonl`
- `mngr usage --format '5h:{five_hour.used_percentage}%/{seven_day.used_percentage}%'`

## Implementing a writer plugin

A writer plugin is responsible for producing `rate_limit_snapshot` events at
the conventional path. The minimal contract is just the JSONL line shape:

```jsonl
{"source":"<your-source>/rate_limits","type":"rate_limit_snapshot","event_id":"evt-<hex>","timestamp":"<ISO 8601>","rate_limits":{"<window-key>":{"used_percentage":<float>,"resets_at":<unix-ts>}}}
```

Append one line per refresh to:

```
<agent_state_dir>/events/<your-source>/rate_limits/events.jsonl
```

`mngr usage` will pick it up automatically -- no plugin registration with this
package required.

The writer chooses both the window keys and (optionally) per-window
`label`s. Keys are used by format templates (`{<key>.used_percentage}`) and
should be identifier-safe if you want format-template support; the per-window
`label` controls human display (e.g. `"5h"` vs the literal key `"five_hour"`).
Render order is the writer's insertion order in the JSONL.
