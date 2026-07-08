# Usage automation scripts

Runnable versions of the recipes documented in
[`../docs/cron_recipes.md`](../docs/cron_recipes.md). That doc explains the
"spare capacity" pace line and the lifecycle logic; these are the same blocks as
committed, executable files so you can point cron / a LaunchAgent straight at
them instead of copy-pasting.

| Script | What it does |
| --- | --- |
| `spare-capacity.sh` | Exit 0 when the account has spare capacity (5h under 80% used **and** weekly under the pace line), non-zero otherwise. A building block the others call. |
| `use-extra.sh` | Run one dedicated agent through the tail of a 5h window, then stop it once the window or weekly pace rolls over. |
| `warm-window.sh` | Fire a throwaway prompt to open a fresh 5h window as soon as the previous one elapses. |
| `dispatch-task.sh` | Pull the next task from a `todo/` queue and launch an agent for it, capped at 2 in flight and only when there's spare capacity. |

## Before you schedule them

Edit the variables at the top of each script for your setup:

- `PROJECT_DIR` — a git repo already trusted in Claude Code (agents are created from its root).
- `AGENT` (`use-extra.sh`) — the dedicated agent's name.
- `TODO_DIR` / `DOING_DIR` / `MAX_PARALLEL` (`dispatch-task.sh`) — the task queue dirs and in-flight cap.

Then wire one up on a schedule (every 5 min shown):

```cron
PATH=/usr/bin:/bin:/home/you/.local/bin
*/5 * * * * /path/to/this/dir/dispatch-task.sh
```

On macOS use a LaunchAgent instead of cron so the scripts can reach your login
Keychain (cron can't) — see the "Scheduling" section of `../docs/cron_recipes.md`
for the plist and `launchctl` commands.

Requires `mngr`, `jq`, `git`, and (for `warm-window.sh` / `use-extra.sh`)
`tmux` and `claude` on `PATH`.
