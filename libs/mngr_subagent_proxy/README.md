# imbue-mngr-subagent-proxy

mngr plugin that owns Claude Code subagents.

When a Claude agent is provisioned, this plugin installs hooks so that
Task tool invocations are routed through a mngr-managed proxy subagent.
This lets you `mngr connect` to subagents, observe their progress, and
the parent still receives a normally-shaped `tool_result`.

## Architecture

- **`PreToolUse:Agent` hook** rewrites the Task tool's `subagent_type`
  to a Haiku dispatcher (`mngr-proxy`) whose Bash tool runs a per-call
  wait-script.
- **Wait-script** invokes `mngr create` for the real subagent (registered
  type `mngr-proxy-child`), then `python -m
  imbue.mngr_subagent_proxy.subagent_wait` which tails the subagent's
  Claude transcript JSONL until `stop_reason=end_turn`. Body is printed
  to stdout followed by a `MNGR_PROXY_END_OF_OUTPUT` sentinel.
- **Haiku** echoes the body verbatim as its final reply, ending its
  turn. Haiku's reply IS the parent's Task `tool_result` (Claude Code's
  PostToolUse `updatedToolOutput` is MCP-only and does not apply to
  built-in tools, so we route via Haiku rather than via PostToolUse).
- **`PostToolUse:Agent` hook** cascade-destroys the subagent, then
  cleans up local state.
- **`SessionStart` hook** reaps orphaned proxy subagents from prior
  sessions (parent crash / Ctrl+C cases).
- **`on_before_agent_destroy`** hookimpl cascade-destroys all recorded
  proxy children before the parent's state dir is wiped.

The plugin also contributes a `mngr-proxy` Claude subagent definition
at `.claude/agents/mngr-proxy.md` and writes per-tool-use wait-scripts
into `$MNGR_AGENT_STATE_DIR/proxy_commands/`.

## Spawned-subagent agent type

Proxy children register the `mngr-proxy-child` agent type. It's
`ClaudeAgent` with one default override: `sync_home_settings=False`,
which stops mngr_claude from copying `~/.claude/{plugins,skills,agents,commands}/`
into the child's per-agent config dir.

## Stop-hook handling

Project-level hooks defined in `.claude/settings.json` and
`.claude/settings.local.json` inherit into spawned subagents because
the subagent shares the parent's worktree (`--transfer=none`) and
Claude Code merges hook arrays across all settings scopes. There is
no Claude Code-side knob to disable a project-level hook from a
higher-precedence scope:

- `disabledPlugins` does not exist
- `enabledPlugins: {}` does not override -- arrays merge, not replace
- `disableAllHooks` cannot override the project scope from below
- `claude --bare` disables every hook, including mngr's own readiness
  hooks (which would break observability of the subagent's transcript)

The plugin handles this at provisioning time by walking three
locations and prepending an env-conditional guard to every
non-mngr Stop / SubagentStop command:

    [ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0; <original>

The wait-script sets `MNGR_SUBAGENT_PROXY_CHILD=1` in the spawned
subagent's env, so guarded hooks no-op there. The parent's env does
not have the var set, so the guard falls through and the original hook
runs normally. Wrapping is idempotent and skips mngr-managed commands
(recognized by `MAIN_CLAUDE_SESSION_ID`, `$MNGR_AGENT_STATE_DIR`,
`wait_for_stop_hook.sh`, `imbue.mngr_subagent_proxy.hooks.`,
`sync_keychain_credentials.py`).

The three locations the plugin auto-guards:

1. The agent's `.claude/settings.local.json`.
2. Every `hooks/hooks.json` file under `~/.claude/plugins/` (where
   Claude Code plugin marketplaces install).
3. Every `hooks/hooks.json` file under each per-agent plugin cache at
   `~/.mngr/agents/*/plugin/claude/anthropic/plugins/` (where Claude
   Code copies the marketplace files at session start; the cache is
   what it actually loads from).

### Gotcha: `.claude/settings.json` (the project-level, git-tracked file)

This is the one place the plugin deliberately does NOT auto-guard:
it's git-tracked, and wrapping any hooks there would dirty the working
tree, which can in turn trigger user-installed "uncommitted changes"
Stop hooks (e.g. `imbue-code-guardian`'s `stop_hook_orchestrator.sh`)
against the parent agent.

To prevent runaway behavior, the plugin instead refuses to provision a
Claude agent that has un-guarded user Stop / SubagentStop hooks in
`.claude/settings.json`. Either:

- Edit the file to start each Stop command with the guard:

      [ -n "$MNGR_SUBAGENT_PROXY_CHILD" ] && exit 0; <original>

- Or pass the `auto_allow_unsafe_project_stop_hooks` agent option to
  bypass the check (intended as a temporary escape hatch; you'll likely
  see runaway autofix loops inside subagents).

### Gotcha: SubagentStop / other event semantics differ across scopes

Top-level vs. subagent semantics for `Stop` and `SubagentStop` aren't
trivially translatable: a user's `SubagentStop` hook may want to fire
when a mngr-managed subagent completes. mngr_subagent_proxy currently
treats both Stop and SubagentStop the same way (env-conditional no-op
on proxy children). If you have nuanced needs, write the env-guard
yourself with a more specific predicate.

### Gotcha: Haiku occasionally re-invokes the wait-script

Haiku's instruction-following on the Bash → end-turn transition isn't
perfect. The wait-script is idempotent: a re-invocation after
PostToolUse cleanup emits just the sentinel and exits 0, so Haiku ends
its turn cleanly instead of error-looping. The cost when this happens
is ~50 cheap Haiku Bash calls before it gives up; not catastrophic
but non-zero.

## Hiding proxy children from `mngr list`

Spawned proxy children are tagged with the label
`mngr_subagent_proxy=child`. To suppress them from listings:

    uv run mngr list --exclude 'labels.mngr_subagent_proxy == "child"'

The inverse — show only proxy children — is useful for debugging:

    uv run mngr list --include 'labels.mngr_subagent_proxy == "child"'

Combine with other filters as usual, e.g. only top-level agents that
are currently running:

    uv run mngr list \
        --exclude 'labels.mngr_subagent_proxy == "child"' \
        --include 'state == "RUNNING"'

## Depth limit

To prevent unbounded nesting, the plugin denies Task at depth
`MNGR_MAX_SUBAGENT_DEPTH` (default 3) with a clear `permissionDecisionReason`.
Override via `--env MNGR_MAX_SUBAGENT_DEPTH=N` at parent create time.

## Tests

Per `CLAUDE.md`, release tests in `test_real_claude_subagent.py` do
NOT run in CI. To run them locally:

    just test libs/mngr_subagent_proxy/imbue/mngr_subagent_proxy/test_real_claude_subagent.py

They require `ANTHROPIC_API_KEY` (or `MNGR_TEST_REAL_CLAUDE_JSON`) and
spawn real Claude agents.
