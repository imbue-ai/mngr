# imbue-mngr-claude-subagent-proxy

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

## Enabling the plugin (disabled by default)

Unlike most mngr plugins -- which load unless you explicitly disable them
-- this plugin is **opt-in: disabled by default**. It does nothing until a
config layer explicitly turns it on:

```toml
[plugins.claude_subagent_proxy]
enabled = true
```

This inverted default exists because the plugin is **very experimental and
breaks a lot of other tooling**: it intercepts Claude Code's built-in
`Task` tool and reroutes subagent execution, which interferes with stop
hooks, plan mode, permission round-trips, and other plugins (see the
warning above and "Deferred / out-of-scope" below). It is too disruptive
to be on for every Claude agent by default, so it must be turned on
deliberately.

The opt-in is enforced in mngr core via the `OPT_IN_PLUGINS` set in
`libs/mngr/imbue/mngr/config/pre_readers.py`: the plugin's name is listed
there, so `read_disabled_plugins()` reports it as disabled unless a config
layer sets `enabled = true`. Setting it to `enabled = true` reuses the
exact same `[plugins.<name>] enabled` key as the normal enable/disable
mechanism, just with the default flipped; later config layers (project,
local) can re-disable it the usual way (`enabled = false`).

## Modes

Once enabled, the plugin has two modes, selected via mngr config (both keys
live under the same table, so set them together):

```toml
[plugins.claude_subagent_proxy]
enabled = true   # required: the plugin is disabled by default (see above).
mode = "PROXY"   # default: route Task calls through a mngr-managed subagent
                 # via a Haiku dispatcher (the original behavior).
# mode = "DENY"  # alternative: deny Task calls with a short skill-pointer
                 # reason that directs Claude at the `mngr-proxy` skill;
                 # the skill teaches the two-command `mngr create` +
                 # `subagent_wait` protocol Claude runs itself via Bash.
```

`PROXY` mode is the default and what the rest of this README describes.
`DENY` mode is documented in its own section below.

## Architecture (PROXY mode)

- **`PreToolUse:Agent` hook** rewrites the Task tool's `subagent_type`
  to a Haiku dispatcher (`mngr-proxy`) whose Bash tool runs a per-call
  wait-script.
- **Wait-script** invokes `mngr create` for the real subagent (registered
  type `mngr-proxy-child`), then `python -m
  imbue.mngr_claude_subagent_proxy.subagent_wait` which tails the subagent's
  Claude transcript JSONL until a terminal `stop_reason` (`end_turn`,
  `stop_sequence`, or `max_tokens`). Body is printed to stdout followed
  by a `MNGR_PROXY_END_OF_OUTPUT` sentinel.
- **Haiku** echoes the body verbatim as its final reply, ending its
  turn. Haiku's reply IS the parent's Task `tool_result` (Claude Code's
  PostToolUse `updatedToolOutput` is MCP-only and does not apply to
  built-in tools, so we route via Haiku rather than via PostToolUse).
- **`PostToolUse:Agent` hook** cascade-destroys the subagent, then
  cleans up local state.
- **`SessionStart` hooks** -- two of them:
  - `hooks/reap.py` reaps orphaned proxy subagents from prior
    sessions (parent crash / Ctrl+C cases).
  - `hooks/guard_stop_hooks.py` wraps every Stop / SubagentStop
    command in this agent's per-agent plugin cache with the
    `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD` env-conditional guard (see the
    "Stop-hook handling" section below). PROXY-only; DENY mode does
    not install this hook.
- **`on_before_agent_destroy`** hookimpl cascade-destroys all recorded
  proxy children before the parent's state dir is wiped.

The plugin also contributes a `mngr-proxy` Claude subagent definition
at `.claude/agents/mngr-proxy/proxy.md` and writes per-tool-use wait-scripts
into `$MNGR_AGENT_STATE_DIR/proxy_commands/`. The subagent is identified by
the `name: mngr-proxy` field in that file's frontmatter, so the enclosing
`mngr-proxy/` subdirectory does not affect discovery (see "Provisioning
artifacts and gitignore" below).

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

    [ -n "$MNGR_CLAUDE_SUBAGENT_PROXY_CHILD" ] && exit 0; <original>

The wait-script sets `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD=1` in the spawned
subagent's env, so guarded hooks no-op there. The parent's env does
not have the var set, so the guard falls through and the original hook
runs normally. Wrapping is idempotent and skips mngr-managed commands
(recognized by `MAIN_CLAUDE_SESSION_ID`, `$MNGR_AGENT_STATE_DIR`,
`wait_for_stop_hook.sh`, `imbue.mngr_claude_subagent_proxy.hooks.`,
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

      [ -n "$MNGR_CLAUDE_SUBAGENT_PROXY_CHILD" ] && exit 0; <original>

- Or set `MNGR_CLAUDE_SUBAGENT_PROXY_ALLOW_UNGUARDED_PROJECT_STOP_HOOKS=1` in
  the env to bypass the check (intended as a temporary escape hatch;
  you'll likely see runaway autofix loops inside subagents).

### Gotcha: SubagentStop / other event semantics differ across scopes

Top-level vs. subagent semantics for `Stop` and `SubagentStop` aren't
trivially translatable: a user's `SubagentStop` hook may want to fire
when a mngr-managed subagent completes. mngr_claude_subagent_proxy currently
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

## Labels and `mngr list` queries

Spawned proxy children are tagged with three labels at create time:

| Label | Value |
|---|---|
| `mngr_claude_subagent_proxy_parent_name` | The parent agent's `MNGR_AGENT_NAME` |
| `mngr_claude_subagent_proxy_parent_id` | The parent agent's `MNGR_AGENT_ID` |
| `mngr_claude_subagent_proxy_tool_use_id` | The originating Claude Code `tool_use_id` |

Top-level agents have none of these labels, so the presence of
`mngr_claude_subagent_proxy_parent_name` (or `_parent_id`) is the
authoritative signal "this is a proxy child".

Useful queries (`mngr list` accepts CEL `--include` / `--exclude`):

    # Hide proxy children from listings.
    uv run mngr list --exclude 'has(labels.mngr_claude_subagent_proxy_parent_name)'

    # Show ONLY proxy children.
    uv run mngr list --include 'has(labels.mngr_claude_subagent_proxy_parent_name)'

    # All children of a specific parent.
    uv run mngr list --include 'labels.mngr_claude_subagent_proxy_parent_name == "my-parent"'

    # Orphaned children whose parent is gone -- combine the labels
    # query with `mngr list` of all current names to subtract:
    uv run mngr list --include 'has(labels.mngr_claude_subagent_proxy_parent_name)' --format json \
        | jq '.agents[] | .labels.mngr_claude_subagent_proxy_parent_name' | sort -u

    # Combine with other filters, e.g. running top-level agents only:
    uv run mngr list \
        --exclude 'has(labels.mngr_claude_subagent_proxy_parent_name)' \
        --include 'state == "RUNNING"'

## DENY mode

Setting `mode = "DENY"` in `[plugins.claude_subagent_proxy]` swaps the proxy
machinery for a single `PreToolUse:Agent` hook plus a Claude skill.
The hook denies every Task call with a short, uniform
`permissionDecisionReason`:

    mngr_claude_subagent_proxy is in deny mode: the Task tool is disabled
    for this agent. Use a mngr-managed subagent instead -- see the
    `mngr-proxy` skill for the two-command spawn-and-wait protocol.

Claude (the calling agent) loads the `mngr-proxy` skill from the
agent's `.claude/skills/`, which teaches the explicit two-command
protocol:

    uv run mngr create '<slug>:<parent_cwd>' \
        --type claude --transfer=none --no-ensure-clean --no-connect \
        --label "mngr_claude_subagent_proxy_parent_name=${MNGR_AGENT_NAME:-}" \
        --label "mngr_claude_subagent_proxy_parent_id=${MNGR_AGENT_ID:-}" \
        --env MNGR_SUBAGENT_DEPTH=$((${MNGR_SUBAGENT_DEPTH:-0}+1)) \
        --message-file <prompt_file>

    uv run python -m imbue.mngr_claude_subagent_proxy.subagent_wait <slug>

Claude writes its own prompt file via the `Write` tool, picks a slug,
runs `mngr create`, then blocks on `subagent_wait` (or backgrounds the
wait via Claude Code's `Bash` `run_in_background=true` and
`BashOutput`s it later). The `subagent_wait` output is
`END_TURN:<reply>`; Claude strips the prefix and uses the rest as the
Task tool's `tool_result`.

The deny hook does NOT generate per-Task wait-scripts or stage prompt
sidefiles -- the skill is the single source of truth and uniform
invocation is cleaner than offering two redundant ways to do the same
thing. If the subagent itself raises a permission dialog,
`subagent_wait` prints a single line `PERMISSION_REQUIRED:<slug>` on
stdout and exits 0 (same exit code as the `END_TURN:` happy path; the
prefix is the signal, not the exit code). Resolve with
`mngr connect <slug>` in another terminal and re-run the same
`subagent_wait` command -- do NOT re-run `mngr create`, the existing
agent is still there. The plugin deliberately does NOT pass `--reuse`
on the spawn command so that slug collisions between concurrent Task
calls surface as hard errors rather than silently merging unrelated
work; pick a fresh unique slug per call.

What deny mode installs:
- `PreToolUse:Agent` hook (the skill-pointer deny).
- `SessionStart` hook -- the same `hooks/reap.py` PROXY uses
  (see "Reaping orphan children" below).
- `.claude/skills/mngr-proxy/SKILL.md` -- the explicit
  spawn-and-wait protocol Claude is expected to use.

What deny mode does NOT install or run (vs. PROXY):
- No `PostToolUse:Agent` cleanup -- the deny hook never runs
  `mngr create` itself, so there is no per-Task-call state on the
  parent to clean up.
- No `mngr-proxy/proxy.md` Haiku dispatcher.
- No `_check_project_settings_stop_hooks_guarded` check on
  `.claude/settings.json`.
- No `hooks/guard_stop_hooks.py` SessionStart hook -- DENY children
  are plain claude agents without the `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD`
  env var, so the guard predicate would never fire.
- No Stop/SubagentStop compatibility checks on spawned children.
- No per-tool_use_id sidefiles under `$MNGR_AGENT_STATE_DIR/`.

Children spawned in deny mode (by Claude following the skill's
protocol) carry the same `mngr_claude_subagent_proxy_parent_*` labels
as PROXY-mode children, so they hide from
`mngr list --exclude 'has(labels.mngr_claude_subagent_proxy_parent_name)'`
and can be listed with the inverse filter.

### Reaping orphan children (shared label-driven)

Both modes install the same `hooks/reap.py` SessionStart hook. It
queries `mngr list` for agents whose
`mngr_claude_subagent_proxy_parent_id` label matches the current
parent's `MNGR_AGENT_ID` and destroys any whose state is terminal
(DONE / STOPPED). RUNNING / WAITING children are left alone (they
may still be doing useful work the user wants to observe). Both
spawn paths attach the label (PROXY's wait-script in `hooks/spawn.py`;
DENY's skill instructs Claude to set it when spawning), so the same
query identifies orphans regardless of mode.

PROXY mode additionally cleans up stale per-tool_use_id sidefiles
under `subagent_map/` etc. in the same reap pass (a no-op in DENY
since those sidefiles are never written). PROXY mode's separate
`hooks/guard_stop_hooks.py` SessionStart hook wraps Stop hooks in
the per-agent plugin cache with the `MNGR_CLAUDE_SUBAGENT_PROXY_CHILD`
env-conditional guard; DENY mode does not install this guard because
DENY-spawned children are plain claude agents without that env var.

## Depth limit

To prevent unbounded nesting, the plugin denies Task at depth
`MNGR_MAX_SUBAGENT_DEPTH` (default 3) with a clear `permissionDecisionReason`.
Override via `--env MNGR_MAX_SUBAGENT_DEPTH=N` at parent create time.

## Tests

Per `CLAUDE.md`, release tests in `test_real_claude_subagent.py` do
NOT run in CI. To run them locally:

    just test libs/mngr_claude_subagent_proxy/imbue/mngr_claude_subagent_proxy/test_real_claude_subagent.py

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

#### Honor agent-definition tool restrictions and system-prompt semantics

**Not implemented.** When a parent calls
`Task(subagent_type="imbue-code-guardian:verify-and-fix", prompt=Y)`
with a specialized agent type, Claude Code's native behavior is to
spawn the subagent with the `.md` definition's body as its **system
prompt** (separate channel from the user message) AND honor the
frontmatter's `tools:` / `model:` declarations (e.g. `tools: [Read,
Grep]` to limit the child to read-only tools).

Today the plugin's typed-`subagent_type` support resolves the agent
definition and inlines the body into the proxy prompt file (PROXY) or
points Claude at the resolved path so it can prepend the body itself
(DENY). Both modes treat the body as **user-message text**, not a
system prompt, and **ignore tool restrictions entirely** -- the
spawned mngr subagent inherits the user's full Claude config. The
v1 docstring on `SubagentProxyMode.DENY` and the `mngr-proxy`
skill both flag this as a known limitation.

Fix path: extend `mngr create` (and the `mngr_claude` Claude
launcher) to accept a `--append-system-prompt-file` sidefile that
threads through to Claude Code's actual `--append-system-prompt` flag
for proper system-prompt semantics. The `.mngr-system-prompt`
convention already exists for the headless agent
(`libs/mngr_claude/imbue/mngr_claude/headless_claude_agent.py`); it
needs to be plumbed through to the interactive `--type claude` path
the proxy uses. For tool restrictions, register
`mngr-proxy-child`-style agent types per frontmatter-declared
permission profile and select the right one at `mngr create` time.

#### Provisioning artifacts and gitignore

The plugin writes one provisioning-time file into the agent's worktree
per mode:

- PROXY mode: `<work_dir>/.claude/agents/mngr-proxy/proxy.md` (the Haiku
  dispatcher agent definition).
- DENY mode: `<work_dir>/.claude/skills/mngr-proxy/SKILL.md` (the
  spawn-and-wait protocol skill).

For git-tracked projects, an unignored file here shows up as untracked
in `git status` and trips clean-tree stop hooks (`imbue-code-guardian`'s
`stop_hook_orchestrator.sh` is the canonical case). Two mitigations are
in place:

1. **Ignorable layout.** Both artifacts live under a `mngr-proxy/`
   subdirectory (rather than flat in `agents/` / `skills/`), so a
   single `.claude/agents/mngr-proxy/` or `.claude/skills/mngr-proxy/`
   line in `.gitignore` covers each. The agent definition is still
   discovered from the subdirectory because Claude Code scans
   `.claude/agents/` recursively and identifies a subagent by its
   frontmatter `name:` field, not its path. (Skills must keep the exact
   `SKILL.md` entry filename and are named by their directory, so the
   skill could not also adopt a `*.local.md` style suffix.)

2. **Gitignore guard.** At provisioning the plugin runs
   `_check_proxy_artifact_gitignored` (which shares the
   `check_path_gitignore_status` helper in `mngr.api.git` with
   mngr_claude's settings.local.json guard) and raises
   `UnignoredProxyArtifactError` if the target path is not gitignored.
   The error tells the user to
   either add the path to `.gitignore` or disable the plugin for the
   repo
   (`mngr config set --scope project plugins.claude_subagent_proxy.enabled false`).
   This fails loudly instead of silently dirtying the worktree.

Fully moving the artifacts *out* of the worktree (so no `.gitignore`
entry is needed at all) is still not possible cleanly. A move was
attempted on an earlier branch and reverted; the investigation findings
are below so the next attempt doesn't redo them:

**The symlink-traversal trap.** mngr_claude's
`_sync_user_resources` (with default `symlink_user_resources=True`)
symlinks the per-agent CLAUDE_CONFIG_DIR's `skills/` and `agents/`
subdirs to `~/.claude/skills/` and `~/.claude/agents/`:

    <state_dir>/plugin/claude/anthropic/skills -> ~/.claude/skills
    <state_dir>/plugin/claude/anthropic/agents -> ~/.claude/agents

So a naive "write to per-agent CLAUDE_CONFIG_DIR" approach
(`<state_dir>/plugin/claude/anthropic/skills/mngr-proxy/SKILL.md`)
follows the symlink and writes to `~/.claude/skills/mngr-proxy/SKILL.md`
-- polluting the user's global Claude config dir, persisting across
all mngr agents, and visible to the user's primary `claude` session.
Worse than worktree pollution, not better. Unit tests using
`FakeHost` will NOT catch this because the fake doesn't replicate
the symlink topology.

**What Claude Code actually supports for non-worktree, non-user-home
placement (as of May 2026):**

- **Skills**: the only escape hatch is the CLI flag `--add-dir <path>`,
  which loads `.claude/skills/` from `<path>` (see
  [Skills from additional directories](https://code.claude.com/docs/en/skills)).
  The matching `additionalDirectories` settings.json key is supposed
  to trigger the same skill discovery but currently doesn't
  ([anthropics/claude-code#37553](https://github.com/anthropics/claude-code/issues/37553)).
  A dedicated `skillsDirectories` setting was proposed and closed as
  duplicate ([#39403](https://github.com/anthropics/claude-code/issues/39403)).
- **Agents**: no escape hatch at all. The docs are explicit -- "Other
  `.claude/` configuration such as subagents, commands, and output
  styles is not loaded from additional directories." Agents resolve
  only from `~/.claude/agents/`, `<project>/.claude/agents/`, or
  plugin-installed (`<plugin>/agents/`).

**Recommended fix paths:**

- For the SKILL.md: plumb `--add-dir <state_dir>` through
  mngr_claude's Claude launcher, write SKILL.md to
  `<state_dir>/.claude/skills/mngr-proxy/SKILL.md` (NOT through
  the symlink). Keeps both the worktree and user home clean. Requires
  changes outside this plugin (mngr_claude's claude invocation).
- For the agent definition: no clean fix until Claude Code grows an
  escape hatch for agent discovery, OR we package the proxy
  definition as a Claude Code plugin installed in the per-agent
  plugin cache (a bigger refactor).

Either way, fully relocating the SKILL.md and agent definition is
**not a small local change** to this plugin; both need work in
mngr_claude or Claude Code itself. Until then, the gitignore guard
above keeps the worktree-resident files from silently dirtying the
tree.

#### Per-agent opt-in via a dedicated agent type

**Not implemented.** Today the plugin opts in at the user/project level
via `[plugins.claude_subagent_proxy] enabled = true` in `settings.toml`,
and its `on_after_provisioning` then fires for *every* `claude` agent
provisioned -- there's no way to enable it for some claude agents but
not others without flipping the global switch. For users who want
DENY (or PROXY) on only specific delegations, that's all-or-nothing.

Better behavior: register a dedicated agent type (e.g.
`claude-deny` / `claude-proxy`) via the `register_agent_type`
hookimpl whose `on_after_provisioning` installs the plugin's hooks,
while plain `claude` agents stay untouched. Users opt in per
invocation with `--type claude-deny`. The current
`mngr-proxy-child` registration in `plugin.py` already follows
the registered-agent-type pattern; this would extend it to the
*top-level* parent as well, instead of mutating every claude agent.

Fix path: add a new `AgentTypeName` constant + child class of
`ClaudeAgentConfig` (or just reuse it), register via
`@hookimpl register_agent_type`, gate the `on_after_provisioning`
body on `isinstance(agent.agent_config, ...)` of that new type
rather than `ClaudeAgentConfig`. Keeps the user's existing
`claude` agents fully unmodified.

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
