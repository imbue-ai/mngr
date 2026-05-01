# imbue-mngr-subagent-proxy

> ⚠️ **EXPERIMENTAL.** This plugin intercepts Claude Code's built-in
> `Task` tool and reroutes subagent execution through mngr-managed
> agents via a Haiku dispatcher. Expect rough edges: there are known
> gaps (plan-mode propagation, stop-instead-of-destroy via mngr GC,
> permission round-trip, etc. -- see "Deferred / out-of-scope" at the
> bottom), and the design depends on Claude Code internals (hook
> protocol, plugin cache layout, marketplace fetch behavior) that may
> shift between Claude Code releases. Watch `mngr list` for orphaned
> children if a session ends abnormally.

mngr plugin that owns Claude Code subagents.

When a Claude agent is provisioned, this plugin installs hooks so that
Task tool invocations are routed through a mngr-managed proxy subagent.
This lets you `mngr connect` to subagents, observe their progress, and
the parent still receives a normally-shaped `tool_result`.

## Modes

The plugin has two modes, selected via mngr config:

```toml
[plugins.subagent_proxy]
mode = "PROXY"   # default: route Task calls through an mngr-managed subagent
                 # via a Haiku dispatcher (the original behavior).
# mode = "DENY"  # alternative: deny Task calls with a copy-pasteable
                 # `mngr create` invocation in the deny reason. Claude
                 # is expected to run those commands itself in Bash.
```

`PROXY` mode is the default and what the rest of this README describes.
`DENY` mode is documented in its own section below.

## Architecture (PROXY mode)

- **`PreToolUse:Agent` hook** rewrites the Task tool's `subagent_type`
  to a Haiku dispatcher (`mngr-proxy`) whose Bash tool runs a per-call
  wait-script.
- **Wait-script** invokes `mngr create` for the real subagent (registered
  type `mngr-proxy-child`), then `python -m
  imbue.mngr_subagent_proxy.subagent_wait` which tails the subagent's
  Claude transcript JSONL until a terminal `stop_reason` (`end_turn`,
  `stop_sequence`, or `max_tokens`). Body is printed to stdout followed
  by a `MNGR_PROXY_END_OF_OUTPUT` sentinel.
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

## Wait-script protocol

`subagent_wait` exits 0 with one of two stdout payloads:

- `END_TURN:<body>` -- the subagent finished its turn. The wait-script
  strips the `END_TURN:` prefix, prints `<body>` followed by
  `MNGR_PROXY_END_OF_OUTPUT`, and Haiku echoes that body verbatim back
  to the parent.
- `PERMISSION_REQUIRED:<target_name>` -- the subagent surfaced a
  permission dialog. The wait-script prints `NEED_PERMISSION:
  <target_name>` and Haiku is instructed to re-run the wait-script
  after a brief `fake_tool` notification; idempotence + a per-tool-use
  watermark sidefile prevent re-firing on the same dialog.

If the subagent is destroyed before completing, `subagent_wait`
returns `END_TURN:[ERROR] mngr subagent destroyed before completion:
<last assistant text>`. The `[ERROR] ` prefix is the only signal the
parent has that the proxy reply represents an error -- Claude Code's
`tool_result.is_error` flag is unreachable from inside Haiku's reply.

End-turn bodies are truncated at `MNGR_SUBAGENT_RESULT_MAX_CHARS`
characters (default 100,000, which roughly matches Claude Code's
native Task `tool_result` truncation) with a `\n\n[truncated]`
suffix. Override via the env var on the parent agent.

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

- Or set `MNGR_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS=1` in
  the env to bypass the check (intended as a temporary escape hatch;
  you'll likely see runaway autofix loops inside subagents).

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

### Gotcha: `WAITING` reported while a child is actively thinking

`mngr list` will report a proxy child as `WAITING` while the child is
in the middle of a long thinking turn -- not because the child is
idle but because the `permissions_waiting` flag set by mngr_claude's
`PermissionRequest` readiness hook is still on the disk between that
hook and the next `PostToolUse`. The flag clears as soon as the
child issues its next tool call. Cosmetic only -- doesn't affect
correctness; the child IS doing useful work. This lives in
mngr_claude's readiness-hook semantics, not this plugin, and is not
on the roadmap to fix.

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

## DENY mode

Setting `mode = "DENY"` in `[plugins.subagent_proxy]` swaps the proxy
machinery for a single `PreToolUse:Agent` hook that DENIES every Task
tool invocation with a copy-pasteable `mngr create` invocation in the
`permissionDecisionReason`. Claude (the calling agent) sees the deny
reason and is expected to run the suggested commands itself via Bash:

```bash
uv run mngr create <slug>:<parent_cwd> \
    --type claude --transfer=none --no-ensure-clean --no-connect \
    --label mngr_subagent_proxy=child \
    --message-file <prompt_file>
uv run python -m imbue.mngr_subagent_proxy.subagent_wait <slug>
```

The wait command prints `END_TURN:<reply>`; Claude strips the prefix
and uses the rest as if it were the Task tool's `tool_result`, then
continues its turn. For `run_in_background=true` Task calls the deny
reason omits the wait step.

What deny mode installs:
- `PreToolUse:Agent` hook only.
- Per-Task-call prompt sidefile under
  `$MNGR_AGENT_STATE_DIR/subagent_prompts/<tool_use_id>.md` so long
  prompts don't have to be embedded in the deny message.

What deny mode does NOT install or run (vs. PROXY):
- No `PostToolUse:Agent` cleanup -- nothing to clean up since the
  plugin never spawns a child.
- No `SessionStart` reaper -- same reason.
- No `mngr-proxy.md` agent definition (no Haiku dispatcher).
- No `_check_project_settings_stop_hooks_guarded` check on
  `.claude/settings.json`.
- No env-conditional Stop-hook guarding of plugin `hooks.json` files.
- No Stop/SubagentStop compatibility checks on spawned children.

Children spawned by Claude in deny mode (via the suggested
`mngr create` invocation) carry the same
`mngr_subagent_proxy=child` label as PROXY-mode children, so they
still hide from `mngr list --exclude 'labels.mngr_subagent_proxy ==
"child"'`.

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

## Deferred / out-of-scope (followup work)

Things that came up during the initial implementation but were
deliberately punted. Listed here so they don't get lost.

### Implementation tasks

#### Plan-mode propagation

**Not implemented.** The wait-script's `mngr create` for the child
doesn't pass through the parent's `--permission-mode plan` flag, so a
parent in plan mode delegates to a non-plan-mode child -- the
read-only guarantee leaks. `test_plan_mode_propagates_to_subagent`
codifies the intended behavior; today it fails. Fix path: read the
parent agent's permission_mode from the spawn hook's invocation
context, forward it via `mngr create --` to the child's claude
invocation.

#### Stop-instead-of-destroy via mngr GC

Today PostToolUse / on_before_agent_destroy / SessionStart-reaper all
**destroy** spawned proxy children once their work is done. The agent
state is gone immediately. This makes mid-iteration recovery
impossible: if a downstream consumer (e.g. autofix orchestration)
later wants to read the child's transcript or `mngr connect` after
the parent's Task call has returned, it can't.

Better behavior: **stop** the children (transition to STOPPED but
keep the state dir + transcript on disk), and let mngr's normal GC
sweep them according to its retention policy. The parent's
`on_before_agent_destroy` cascade-destroy can stay as a fallback for
the actual cleanup. This requires plumbing through a "stop" action
in `destroy_agent_detached` / `destroy_worker.py` that calls
`mngr stop` rather than `mngr destroy`.

#### Transparent permission resolution (Option B)

Today, when a spawned child raises a permission dialog, the proxy
relays a `NEED_PERMISSION` line to the parent and the user has to
`mngr connect <child>` in another terminal to resolve it. The
already-considered-and-deferred Option B (see plan): a wrapper that
intercepts the dialog at the parent level and round-trips the
decision back to the child. Defers all the way back to the original
plan; not picked up here for simplicity.

#### Tighter mngr_recursive integration

`mngr_binary.py` was copied from `mngr_recursive.watcher_common.get_mngr_command`
with a `uv run mngr` fallback (see module docstring). If
mngr_recursive grows another caller that diverges from our copy, the
two will need to be re-synced. Better: extract a thin shared package
or add an mngr core helper both can depend on.

### Validation tasks

#### Backgrounded autofix end-to-end

`run_in_background: true` Task calls have a release test
(`test_task_run_in_background_returns_immediately`) but it has not
been live-validated yet. Real-world quick-check happened
incidentally when a plan-mode-test subagent was spawned in
background mode and the proxy correctly returned poll handles --
that's encouraging but not equivalent to running the full release
test.

#### Parallel background tasks (N=3+)

N independent mngr children sharing the same parent's
`subagent_map/`, `proxy_commands/`, watermark sidefiles. Unit-tested
for shape but not raced live. Probably resilient because each
tool_use_id owns its own sidefile prefix, but worth verifying.

#### mngr_recursive end-to-end

Our `get_mngr_command` happily prefers `$UV_TOOL_BIN_DIR/mngr` when
mngr_recursive has provisioned one, but the integrated path has only
been unit-tested -- never run with `mngr_recursive` actually enabled
on the parent.

#### Session-id resume

Unit-tested for the read path; never verified live with a real
`/resume` mid-Task.
