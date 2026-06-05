Added a `[tmux]` configuration section for customizing the tmux sessions mngr runs agents in:

- `tmux.attach_args` -- extra tmux client flags inserted before the `attach` subcommand when connecting to an agent (`tmux <attach_args> attach ...`). The motivating case is `["-CC"]` for iTerm2 control mode (native tabs/windows); `-u` / `-2` also work. Applies to both local and remote (SSH) agents.
- `tmux.user_config_path` -- an extra tmux config file sourced into every mngr session. Unlike the auto-generated `~/.mngr/tmux.conf`, this file is never overwritten, so it is a stable place for mngr-session-specific tmux config.
- `tmux.primary_window_name` (default `agent`) -- mngr now names the agent's primary window and targets it by name instead of the literal `:0` index, so mngr works regardless of the user's tmux `base-index` setting.

See `docs/tmux_users.md` for usage.
