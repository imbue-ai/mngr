# imbue-mngr-subagent-proxy

mngr plugin that owns Claude Code subagents.

When a Claude agent is provisioned, this plugin installs hooks and helper
scripts into the agent so that Task tool invocations are routed through a
mngr-managed proxy subagent. This lets mngr observe, gate, and rewrite
subagent results rather than letting Claude Code spawn them opaquely.

The plugin contributes:

- `PreToolUse` and `PostToolUse` hooks on the `Agent` (Task) tool that spawn
  a proxy and rewrite its result.
- A `SessionStart` hook that reaps orphaned proxy subagents.
- A `mngr-proxy` Claude subagent definition at `.claude/agents/mngr-proxy.md`.
- Per-tool-use wait-scripts at `$MNGR_AGENT_STATE_DIR/proxy_commands/wait-<tool_use_id>.sh`, generated on demand by the spawn hook for the Haiku proxy agent's Bash tool.

## Hiding proxy children from `mngr list`

Spawned proxy children are tagged with the label `mngr_subagent_proxy=child`. To suppress them from your default listing, exclude on that label:

    uv run mngr list --exclude 'labels.mngr_subagent_proxy == "child"'

Or alias that as `mngr list-mine` in your shell. Use `mngr list` (no filter) when you need to see the full tree, e.g. while debugging an in-flight Task.
