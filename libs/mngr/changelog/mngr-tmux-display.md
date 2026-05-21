Show the full agent name in the tmux status bar.

User-visible changes:

- mngr's generated tmux config (`~/.mngr/tmux.conf`) now sets
  `status-left-length` to 20 so a full `mngr-...` session name shows in the
  status bar. Previously tmux's default of 10 truncated names like
  `mngr-tmux-display` to `[mngr-tmux`, with the window list mashed onto the end.
- The widening is written before the user's `~/.tmux.conf` is sourced, so a
  `status-left-length` set in the user's own config overrides it.
