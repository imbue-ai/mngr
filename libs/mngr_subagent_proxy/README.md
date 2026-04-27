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

## Stop-hook handling in spawned subagents

Project-level hooks defined in `.claude/settings.json` and
`.claude/settings.local.json` inherit into spawned subagents because
the subagent shares the parent's worktree (`--transfer=none`) and
Claude Code merges hook arrays across all settings scopes. There is
no Claude Code-side knob to disable a project-level hook from a
higher-precedence scope (`disabledPlugins` does not exist;
`disableAllHooks` does not override the project scope; `--bare`
disables every hook including mngr's own readiness hooks).

The plugin handles this at provisioning time by walking every
existing Stop / SubagentStop command in `settings.local.json` and
prepending the env-conditional guard:

    [ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0; <original>

The wait-script sets `MNGR_SUBAGENT_PROXY_CHILD=1` in the spawned
subagent's env, so user-defined Stop hooks no-op there. The parent's
env does not have the var set, so the guard falls through and the
original hook runs normally. Wrapping is idempotent (re-runs detect
the prefix) and skips hooks already recognized as mngr-managed.

This does not catch hooks installed via a Claude Code plugin's
own `hooks/hooks.json` (e.g. `imbue-code-guardian`), since those
are loaded by the plugin runtime and not visible in
`settings.local.json`. To self-guard a plugin's Stop hook, edit the
plugin's `hooks/hooks.json` upstream to start each Stop command with:

    [ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0
    # ... rest of your Stop hook ...

## Hiding proxy children from `mngr list`

Spawned proxy children are tagged with the label `mngr_subagent_proxy=child`. To suppress them from your default listing, exclude on that label:

    uv run mngr list --exclude 'labels.mngr_subagent_proxy == "child"'
