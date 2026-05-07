# imbue-mngr-usage

`mngr usage` — scriptable visibility into Claude Code's rolling 5-hour and 7-day quota windows.

## What it does

Claude Code (CLI) shows quota usage interactively via the `/usage` slash command. The data
comes from API response headers (`anthropic-ratelimit-unified-...`) on every call, and is not
otherwise persisted to disk. This plugin makes the same data available to shell pipelines.

## How it works

A statusline shim is installed into each per-agent Claude config; whenever Claude Code
renders its statusline it pipes a JSON snapshot (including `rate_limits`) to the shim,
which atomically merges the rate-limit fields into a shared cache file at
`<profile_dir>/usage/claude_rate_limits.json`. The shim composes with any pre-existing
user `statusLine.command`, so caveman / starship / etc. keep working unchanged.

`mngr usage` is purely a reader -- it never spawns Claude or hits the API, so it incurs
no Anthropic charges. If the cache is empty (no interactive Claude session has rendered
yet under this profile) it prints an actionable hint instead of empty windows.

## Output

`mngr usage` supports the same output ergonomics as `mngr list`:

- `mngr usage` (human)
- `mngr usage --format json`
- `mngr usage --format jsonl`
- `mngr usage --format '5h:{five_hour.used_percentage}%/{seven_day.used_percentage}%'`
