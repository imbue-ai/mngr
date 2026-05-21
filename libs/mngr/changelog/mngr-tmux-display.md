Show the full agent name in the tmux status bar.

User-visible changes:

- When an agent's tmux session is created, mngr now widens tmux's
  `status-left-length` to fit the full session name. Previously tmux's default
  of 10 characters truncated names like `mngr-tmux-display` to `[mngr-tmux`,
  with the window list mashed onto the end. The width is capped at 40 so a very
  long agent name cannot crowd out the window list, and is only ever raised --
  never lowered -- so a user's custom tmux status bar is left intact.
