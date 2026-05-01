---
name: mngr-subagents
description: How to delegate work to a mngr-managed subagent via Bash on this agent. The native Task tool is disabled in favor of mngr; this skill explains the protocol for spawning a subagent and capturing its reply so you can continue your turn.
---

# mngr subagents

The `mngr_subagent_proxy` plugin is configured in DENY mode on this
agent. The native `Task` tool is intentionally disabled. Use this
skill instead of calling `Task` whenever you would normally delegate
to a subagent. The protocol below replaces the entire `Task` workflow.

## How to spawn a mngr subagent

Two Bash commands. First, spawn:

    uv run mngr create '<slug>:<parent_cwd>' \
        --type claude --transfer=none --no-ensure-clean --no-connect --reuse \
        --label mngr_subagent_proxy=child \
        --message-file <prompt_file>

- `<slug>` is a short, agent-name-friendly identifier for what the
  subagent is doing (e.g. `code-review`, `find-readmes`). Make it
  unique across concurrent calls -- a short suffix like the date or a
  random hex is fine.
- `<parent_cwd>` is the absolute path of the directory you're working
  in (use the value you'd pass to `cd`); pins the subagent's worktree
  base to where you are.
- `<prompt_file>` is a file containing the prompt you would have
  passed to `Task`. Write it with the `Write` tool first; this avoids
  shell-escaping every newline / backtick / quote in the prompt body.
- `--label mngr_subagent_proxy=child` tags it so it hides from
  `mngr list` and gets reaped on parent destroy.
- `--reuse` makes the create idempotent if a previous attempt
  partially succeeded; safe to retry the same command.

Second, block until the subagent ends its turn and capture its reply:

    uv run python -m imbue.mngr_subagent_proxy.subagent_wait <slug>

This prints a single line of the form `END_TURN:<reply>` on success.
Strip the literal `END_TURN:` prefix; the rest is the subagent's
final reply. Treat it as the `tool_result` you would have received
from `Task` and continue your own turn.

If you don't want to block on the wait, invoke the second command via
the `Bash` tool's own `run_in_background=true` parameter and
`BashOutput` it later when you need the reply. There is no separate
DENY-specific background flag -- Claude Code's existing Bash
backgrounding handles this.

## Convenience: per-Task wait-script (if you do call Task)

You may still call `Task` if you forget or it's the most natural shape
for your turn. The plugin will deny it with a `permissionDecisionReason`
that points at a per-Task-call wait-script:

    bash <wait_script_path>

That script does the same `mngr create` + `subagent_wait` sequence
described above with all the boilerplate (env capture, target name,
`--message-file` setup) baked in. Its stdout is the subagent's reply,
already-stripped of `END_TURN:`. Use it just like the protocol above.
This is purely a convenience -- the explicit two-command form is the
canonical interface and is preferred when you already know what you
want to delegate.

## Permission dialogs

If the spawned subagent itself raises a permission dialog,
`subagent_wait` exits non-zero and prints `NEED_PERMISSION: <name>`.
Tell the user to run `mngr connect <name>` in another terminal to
resolve, then re-run the same `subagent_wait` command. The spawn step
is idempotent under `--reuse`, so you do not need to repeat it.

## Inspecting a running subagent

Independent of the wait, the user (or you, on request) can:

    mngr connect <slug>                                       # interactive TUI
    mngr transcript <slug>                                    # full message log
    mngr list --include 'labels.mngr_subagent_proxy == "child"'

## What NOT to do

- Do not retry `Task` after a deny -- the plugin will deny it again.
- Do not `tail -f` the subagent's output file or invent your own
  polling. `subagent_wait` is the supported wait primitive.
- Do not skip the `--label mngr_subagent_proxy=child` flag; without
  it, the subagent will not hide from `mngr list` and will not be
  reaped automatically.
