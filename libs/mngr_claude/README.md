# imbue-mngr-claude

Claude agent type plugin for [mngr](../../README.md).

Provides the `claude`, `code-guardian`, and `fixme-fairy` agent types.

## Shared `CLAUDE_CONFIG_DIR` (local agents)

By default, every Claude agent gets its own per-agent config directory under
`$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/` (populated by copying/symlinking
from `~/.claude/`). Set `use_env_config_dir = true` on the agent type config to
have local Claude agents share the user's `$CLAUDE_CONFIG_DIR` instead:

```toml
[agent_types.claude]
use_env_config_dir = true
```

When enabled:

- `$CLAUDE_CONFIG_DIR` **must** be set in the parent shell; mngr errors out otherwise.
- Only local hosts are supported.
- mngr never writes to the user's Claude config (no trust additions, no dialog
  dismissal, no per-agent settings.json, no keychain provisioning). The user is
  responsible for one-time interactive `claude` setup (trust the work_dir,
  complete onboarding, log in).
- Other sync/override/auto-dismiss fields on the agent config are silently
  ignored since shared mode has no per-agent dir to write into.

## Approximate response streaming (`streaming_snapshot_interval_seconds`)

Set `streaming_snapshot_interval_seconds` on the agent type config to get an
*approximate*, live view of Claude's in-progress assistant text:

```toml
[agent_types.claude]
streaming_snapshot_interval_seconds = 0.25
```

When the value is `> 0`, a background watcher periodically captures the agent's
tmux pane, reverse-maps the rendered assistant text back into markdown, and
writes it to `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer`. When the value
is `<= 0` (the default), the watcher is neither provisioned nor run.

`stream_buffer` format:

- Line 1: the id (`uuid`) of the last *complete* assistant message (empty string
  if none yet), so consumers can tell a genuinely-new streaming message apart
  from leftover text right after a message finished.
- Lines 2+: the in-progress assistant text, reverse-mapped to markdown.

Notes:

- This is best-effort and approximate. It reconstructs bold/italic, inline code,
  links, blockquotes, lists, code blocks, and tables from the terminal rendering;
  heading levels and code-block languages are not recoverable.
- The body is strict-append within a message (snapshots are overlap-stitched) and
  is emptied when the agent goes idle. A table is held back until it stops
  changing across polls, so it appears once rendered rather than row-by-row.
- The watcher is a Python script; the agent host must have `python3` available
  (provisioning fails fast if it does not).
