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
