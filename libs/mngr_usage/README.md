# imbue-mngr-usage

`mngr usage` — agent-agnostic CLI for rolling-window usage / quota data.

## What it does

Provides a single `mngr usage` command that surfaces rolling-window usage (e.g.
Claude.ai's 5-hour, 7-day, and overage quota windows) in human / json / jsonl /
format-template output. The command itself knows nothing about any specific agent
type; it dispatches to data-providing plugins via a pluggy hook.

## Architecture

This package contains:

- The `mngr usage` CLI command and its rendering helpers.
- The `current_usage_snapshot` hookspec (in `hookspecs.py`).
- The `UsageSnapshot` and `WindowSnapshot` data types.

Provider plugins implement the hookspec to surface their own data:

- `imbue-mngr-claude-usage` — Claude.ai rate-limit data, captured via a
  per-agent statusline shim and read from each agent's
  `events/claude/rate_limits/events.jsonl`.

When multiple providers contribute, the freshest snapshot (largest `updated_at`)
is rendered.

## Output formats

- `mngr usage` (human, with stale warning when applicable)
- `mngr usage --format json`
- `mngr usage --format jsonl`
- `mngr usage --format '5h:{five_hour.used_percentage}%/{seven_day.used_percentage}%'`

## Implementing a provider

```python
from imbue.mngr_usage.data_types import UsageSnapshot, WindowSnapshot
from imbue.mngr import hookimpl


@hookimpl
def current_usage_snapshot(mngr_ctx) -> UsageSnapshot | None:
    # Read whatever data source you own, return the freshest snapshot.
    return UsageSnapshot(
        source_name="myprovider",
        windows={"five_hour": WindowSnapshot(used_percentage=42.0, resets_at=1778280000)},
        updated_at=1778270000,
    )
```
