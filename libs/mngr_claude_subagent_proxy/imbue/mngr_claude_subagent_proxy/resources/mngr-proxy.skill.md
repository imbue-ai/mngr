---
name: mngr-proxy
description: How to delegate work to a mngr-managed subagent via Bash on this agent. The native Task tool is disabled in favor of mngr; this skill explains the protocol for spawning a subagent and capturing its reply so you can continue your turn.
---

# mngr subagents

The `mngr_claude_subagent_proxy` plugin is configured in DENY mode on
this agent. The native `Task` tool is intentionally disabled. Use this
skill instead of calling `Task` whenever you would normally delegate
to a subagent. The protocol below replaces the entire `Task` workflow.

## How to spawn a mngr subagent

Two Bash commands. First, spawn:

    uv run mngr create '<slug>:<parent_cwd>' \
        --type claude --transfer=none --no-ensure-clean --no-connect \
        --label "mngr_claude_subagent_proxy_parent_name=${MNGR_AGENT_NAME:-}" \
        --label "mngr_claude_subagent_proxy_parent_id=${MNGR_AGENT_ID:-}" \
        --env MNGR_SUBAGENT_DEPTH=$((${MNGR_SUBAGENT_DEPTH:-0}+1)) \
        --message-file <prompt_file>

- `<slug>` is a short, agent-name-friendly identifier for what the
  subagent is doing (e.g. `code-review`, `find-readmes`). Make it
  unique across concurrent calls -- a short suffix like the date or a
  random hex is fine. The plugin does NOT pass `--reuse`, so a
  duplicate slug is a hard error and forces you to pick a new one;
  this is intentional (see "What NOT to do" below for why).
- `<parent_cwd>` is the absolute path of the directory you're working
  in (use the value you'd pass to `cd`); pins the subagent's worktree
  base to where you are.
- `<prompt_file>` is a file containing the prompt you would have
  passed to `Task`. Write it with the `Write` tool first; this avoids
  shell-escaping every newline / backtick / quote in the prompt body.
- The two `mngr_claude_subagent_proxy_parent_*` labels carry parent
  linkage so the user can filter the child out of `mngr list`
  (`--exclude 'has(labels.mngr_claude_subagent_proxy_parent_name)'`)
  and identify which parent spawned it. Leave the literal
  `${MNGR_AGENT_NAME:-}` / `${MNGR_AGENT_ID:-}` expansions in the
  command -- the shell substitutes them at run time.
- `--env MNGR_SUBAGENT_DEPTH=...` propagates the parent's nesting
  depth (incremented by one) to the child so the plugin's depth-limit
  guard fires after `MNGR_MAX_SUBAGENT_DEPTH` levels of nesting.
  Leave the literal `$((${MNGR_SUBAGENT_DEPTH:-0}+1))` arithmetic in
  the command -- the shell evaluates it; do not pre-substitute a
  numeric value yourself.

Second, block until the subagent ends its turn and capture its reply:

    uv run python -m imbue.mngr_claude_subagent_proxy.subagent_wait <slug>

This prints a single line of the form `END_TURN:<reply>` on success.
Strip the literal `END_TURN:` prefix; the rest is the subagent's
final reply. Treat it as the `tool_result` you would have received
from `Task` and continue your own turn.

If you don't want to block on the wait, invoke the second command via
the `Bash` tool's own `run_in_background=true` parameter and
`BashOutput` it later when you need the reply. There is no separate
DENY-specific background flag -- Claude Code's existing Bash
backgrounding handles this.

If you do call `Task`, the plugin denies it with a reminder pointing
back at this skill -- there is no separate wait-script convenience
path. The two-command form above is the canonical and only interface.

## Typed `subagent_type` (e.g. `imbue-code-guardian:verify-and-fix`)

When you would have called `Task(subagent_type="X", prompt=Y)` with a
specialized agent type (anything other than built-ins like
`general-purpose` or `Explore`), Claude Code normally spawns the
subagent with the system prompt baked into the agent definition file
plus `Y` as the user message. The mngr subagent flow only has one
input channel (`mngr create --message-file`), so you must inline the
system prompt yourself: prepend the body of the agent definition `.md`
file to your prompt file before running `mngr create`.

The plugin's deny reason names the resolved path when it finds one.
Resolution branches on whether the `subagent_type` contains a `:`:

- Plugin-namespaced (`plugin:agent`): only
  `~/.claude/plugins/marketplaces/*/plugins/<plugin>/agents/<agent>.md`
  is checked.
- Non-namespaced: `<work_dir>/.claude/agents/<name>.md` then
  `~/.claude/agents/<name>.md` (project-local wins). The flat
  agents/ directories are NOT a fallback for namespaced types.

Write the prompt file like:

    # System prompt for subagent_type 'imbue-code-guardian:verify-and-fix'

    <verbatim body of the .md file, after the YAML frontmatter>

    # Task from parent

    <your original prompt>

For built-in types (`general-purpose`, `Explore`, ...) there is no
on-disk definition; just write the prompt as-is. The deny reason
omits the typed-subagent pointer in that case.

Known v1 limitation: tool restrictions declared in the agent
definition frontmatter (`tools: [Read, Grep]`, etc.) are NOT honored
-- the spawned mngr subagent inherits the user's full Claude config.
If the original subagent's value depended on those restrictions, flag
that explicitly to the user rather than silently spawning a broader
subagent.

## Permission dialogs

If the spawned subagent itself raises a permission dialog,
`subagent_wait` prints a single line `PERMISSION_REQUIRED:<slug>` on
stdout and exits 0 (the same exit code as the `END_TURN:` happy path).
Detect the permission case by the `PERMISSION_REQUIRED:` prefix on the
output, not by the exit code. Tell the user to run `mngr connect <slug>`
in another terminal to resolve, then re-run the same `subagent_wait`
command. Do NOT re-run `mngr create` -- the existing agent is still
there, only the wait needs to resume.

## Inspecting a running subagent

Independent of the wait, the user (or you, on request) can:

    mngr connect <slug>                                                              # interactive TUI
    mngr transcript <slug>                                                           # full message log
    mngr list --include 'has(labels.mngr_claude_subagent_proxy_parent_name)'         # all proxy children

## What NOT to do

- Do not retry `Task` after a deny -- the plugin will deny it again.
- Do not `tail -f` the subagent's output file or invent your own
  polling. `subagent_wait` is the supported wait primitive.
- Do not skip the `mngr_claude_subagent_proxy_parent_*` labels; without
  them, the subagent will not be identifiable as a proxy child via
  `mngr list` filters.
- If `mngr create` fails with "agent already exists", pick a NEW unique
  slug and retry. Do NOT destroy the existing agent -- it may belong
  to a concurrent call (or an earlier call you have not yet collected
  the reply from), and destroying it would lose that work. The plugin
  intentionally does not pass `--reuse` so that slug collisions fail
  loudly rather than silently merging unrelated work.
