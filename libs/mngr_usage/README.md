# imbue-mngr-usage

`mngr usage` — scriptable visibility into Claude Code's rolling 5-hour and 7-day quota windows.

## What it does

Claude Code (CLI) shows quota usage interactively via the `/usage` slash command. The data
comes from API response headers (`anthropic-ratelimit-unified-...`) on every call, and is not
otherwise persisted to disk. This plugin makes the same data available to shell pipelines.

## How it works

Two writers populate a shared cache file at `<profile_dir>/usage/claude_rate_limits.json`:

1. A statusline shim is installed into each per-agent Claude config; whenever Claude Code
   refreshes its statusline it pipes the rate-limit JSON to the shim, which atomically merges
   it into the cache (free, fires often during normal Claude Code use).
2. The `mngr usage` command reads the cache, and when stale spawns a brief `claude -p` call
   to refresh (configurable; ~$0.005 per refresh).

## Output

`mngr usage` supports the same output ergonomics as `mngr list`:

- `mngr usage` (human)
- `mngr usage --format json`
- `mngr usage --format jsonl`
- `mngr usage --format '5h:{five_hour.used_percentage}%/{seven_day.used_percentage}%'`
