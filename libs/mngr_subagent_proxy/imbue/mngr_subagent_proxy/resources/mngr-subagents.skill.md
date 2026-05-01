---
name: mngr-subagents
description: Use this skill whenever the Task tool is denied by mngr_subagent_proxy. Explains how to delegate work to a mngr-managed subagent via Bash and capture its reply, then continue the parent turn.
---

# mngr subagents

The `mngr_subagent_proxy` plugin is configured in DENY mode on this
agent. The native `Task` tool is intentionally disabled. Whenever you
would normally call `Task`, your call is denied and the deny reason
points you at a concrete `bash` command. Run that command instead of
giving up, retrying `Task`, or improvising your own monitoring.

## Synchronous subagent (default)

The deny reason includes a path to a per-Task-call wait-script:

    bash <wait_script_path>

Run it once via the `Bash` tool. The script:

1. Spawns a mngr-managed subagent (named
   `<parent>--subagent-<slug>-<tid>`, label `mngr_subagent_proxy=child`)
   with the Task prompt delivered via `--message-file`.
2. Blocks until the subagent ends its turn.
3. Prints the subagent's final reply to stdout and exits 0.

**The script's stdout IS the subagent's reply.** Use it as the
`tool_result` you would have received from `Task` and continue your
turn. Do not invent your own polling, do not `tail -f` the agent's
output file, do not re-run `mngr create` yourself: the wait-script
already does spawn + wait + print in a single call.

## Background subagent (`run_in_background=true`)

When the original Task call had `run_in_background=true`, the deny
reason gives you the same wait-script path with a `--spawn-only` flag:

    bash <wait_script_path> --spawn-only

This spawns the subagent and exits immediately. Continue your turn;
the subagent runs to completion in the background. Inspect it later
via `mngr connect <name>` or `mngr transcript <name>`.

## Permission dialogs

If the subagent itself raises a permission dialog the script exits
non-zero and prints `NEED_PERMISSION: <name>`. Tell the user to run
`mngr connect <name>` in another terminal to resolve, then re-run the
exact same wait-script command. The script is idempotent and will not
re-spawn the subagent on the second call.

## Inspecting a running subagent

Independent of the wait-script, the user (or you, on request) can:

    mngr connect <subagent_name>      # interactive TUI
    mngr transcript <subagent_name>   # full message log
    mngr list --include 'labels.mngr_subagent_proxy == "child"'
