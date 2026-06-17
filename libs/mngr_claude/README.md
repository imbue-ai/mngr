# imbue-mngr-claude

Claude agent type plugin for [mngr](https://github.com/imbue-ai/mngr).

Provides the `claude`, `code-guardian`, and `fixme-fairy` agent types.

## Shared `CLAUDE_CONFIG_DIR` (local agents)

By default, every Claude agent gets its own per-agent config directory (populated
from `~/.claude/`). Set `use_env_config_dir = true` to have local Claude agents
share the user's `$CLAUDE_CONFIG_DIR` instead:

```toml
[agent_types.claude]
use_env_config_dir = true
```

When enabled:

- `$CLAUDE_CONFIG_DIR` must be set in the parent shell; mngr errors out otherwise.
- Only local hosts are supported.
- mngr uses your config dir as-is and does not modify it; you just need to be
  logged in to Claude.
- Other sync/override/auto-dismiss fields are ignored in this mode.

## Approximate response streaming (`streaming_snapshot_interval_seconds`)

Set `streaming_snapshot_interval_seconds` to get an approximate, live view of
Claude's in-progress assistant text:

```toml
[agent_types.claude]
streaming_snapshot_interval_seconds = 0.25
```

When the value is `> 0`, a background watcher periodically writes the in-progress
assistant text to a stream buffer. When `<= 0` (the default), it does not run.

Limitations:

- The snapshot is best-effort and approximate; heading levels and code-block
  languages are not recoverable.
- The agent host must have `python3` available (provisioning fails fast otherwise).
