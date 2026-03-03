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
name = "logs"
command = "mng logs $MNG_AGENT_NAME"
refresh_afterwards = true
```

Each entry defines a keybinding (the table key, e.g. `c`) that appears in the status bar and runs with the `MNG_AGENT_NAME` environment variable set to the focused agent's name. Custom commands override builtins when they share the same key. Set `enabled = false` to disable a builtin.
