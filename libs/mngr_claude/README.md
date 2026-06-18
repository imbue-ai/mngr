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

## Version pinning and auto-updates

Pin the Claude Code version that gets installed, and control its background
auto-updater:

```toml
[agent_types.claude]
version = "2.1.50"      # install this exact version; provisioning verifies it
update_policy = "NEVER" # disable claude's auto-updater so the pin sticks
```

- `version` — pin the Claude Code version to install (the official installer is run
  with `bash -s <version>`); provisioning verifies the installed binary matches and
  errors on a mismatch. Default: unset (latest).
- `update_policy` — govern Claude Code's background auto-updater: `NEVER` sets
  `DISABLE_AUTOUPDATER=1` in the agent environment so the installed binary stays put,
  `AUTO` leaves the auto-updater enabled, and `ASK` behaves like `AUTO` (claude has no
  interactive update flow). When unset (the default), it resolves to `NEVER`
  (auto-update disabled) so a managed agent stays on its installed version — set
  `AUTO` to opt back into Claude Code's auto-updater. Ignored in `use_env_config_dir`
  (shared) mode, where mngr leaves your claude environment alone. (Note: before this,
  mngr did *not* disable claude's auto-updater on local agents — it inherited your
  `~/.claude.json` `autoUpdates` value, so local agents typically auto-updated.)

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
