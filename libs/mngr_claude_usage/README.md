# imbue-mngr-claude-usage

Claude data provider for `mngr usage`. Two responsibilities:

1. **Capture**: implements `claude_extra_per_agent_settings` (from `mngr_claude`) to install
   a tiny statusline shim into each per-agent `<work_dir>/.claude/settings.local.json`. The
   shim wraps any pre-existing user `statusLine.command` and forwards the JSON snapshot
   Claude Code sends on every render to a per-agent rate-limits events file at
   `$MNGR_AGENT_STATE_DIR/events/claude/rate_limits/events.jsonl`. After the first
   successful API response of the session, that snapshot includes `rate_limits`
   (Claude.ai subscriptions only); we emit one JSONL event per render.

2. **Read**: implements `current_usage_snapshot` (from `mngr_usage`) by walking all
   per-agent rate-limits events files on the host and returning the freshest event
   reshaped as a `UsageSnapshot`.

The `mngr usage` CLI itself lives in `imbue-mngr-usage` and is agent-agnostic;
it just calls the hook and renders. Other agent types can plug in by implementing
the same hook against their own data.

## How the pieces fit

```
Claude Code statusline render (every turn)
  └─→ <work_dir>/.claude/settings.local.json's statusLine.command
        └─→ claude_statusline.sh  (our shim)
              ├─→ claude_rate_limits_writer.sh
              │     └─→ events/claude/rate_limits/events.jsonl  (append one event)
              └─→ user's pre-existing statusline command  (chain through)

mngr usage
  └─→ pluggy.hook.current_usage_snapshot
        └─→ this plugin: walk agents, read events, return freshest snapshot
```
