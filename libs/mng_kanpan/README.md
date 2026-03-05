# Kanpan

All-seeing agent tracker. The name combines Sino-Japanese 看 (*kan*, "to look", as in 看板 *kanban*) and Greek πᾶν (*pan*, "all") -- a unified view that aggregates state from all sources (mng agent lifecycle, git branches, GitHub PRs and CI) into a single board.

Launch with `mng kanpan`. Requires the `gh` CLI to be installed and authenticated.

## Custom commands

Add to your mng settings file (e.g. `.mng/settings.toml`):

```toml
[plugins.kanpan.commands.c]
name = "connect"
command = "mng connect $MNG_AGENT_NAME"

[plugins.kanpan.commands.l]
name = "events"
command = "mng events $MNG_AGENT_NAME"
refresh_afterwards = true
```

Each entry defines a keybinding (the table key, e.g. `c`) that appears in the status bar and runs with the `MNG_AGENT_NAME` environment variable set to the focused agent's name. Custom commands override builtins when they share the same key. Set `enabled = false` to disable a builtin.

## Refresh cooldown

Kanpan rate-limits refreshes to avoid hammering the GitHub API. Two cooldown durations are configurable:

```toml
[plugins.kanpan]
# Minimum seconds before retrying after a failed full refresh
auto_refresh_cooldown_seconds = 60.0
# Minimum seconds between manual refreshes (pressing 'r')
manual_refresh_cooldown_seconds = 5.0
```

When a manual refresh is requested within the cooldown window, it is deferred until the cooldown expires. A manual refresh with a shorter cooldown will replace a pending automatic refresh if it would fire sooner. Operations like delete, push, and custom commands trigger a cheap local-only refresh that bypasses cooldown (no GitHub API calls).
