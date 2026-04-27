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

## Stop hooks fire inside spawned subagents

Project-level hooks defined in `.claude/settings.json` (and any
plugin-installed hooks) inherit into spawned subagents because the
subagent shares the parent's worktree (`--transfer=none`) and
Claude Code merges hook arrays across all settings scopes. There is
currently no Claude Code mechanism to disable a project-level hook
from a higher-precedence scope. `claude --bare` disables hooks but
also disables mngr's own readiness hooks, so we don't use it.

To prevent your own Stop hooks from re-prompting a spawned subagent
into autofix/verify cycles (the most common breakage), guard them
on the env var `MNGR_SUBAGENT_PROXY_CHILD`, which the proxy sets to
`1` for spawned subagents:

    [ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0
    # ... rest of your Stop hook ...

## Hiding proxy children from `mngr list`

Spawned proxy children are tagged with the label `mngr_subagent_proxy=child`. To suppress them from your default listing, exclude on that label:

    uv run mngr list --exclude 'labels.mngr_subagent_proxy == "child"'
