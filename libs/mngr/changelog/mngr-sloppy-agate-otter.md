Fixed an additional tmux window whose command is too long to type never running it. `mngr` types
each additional window's command into the pane with `send-keys`, and a pane's pty is still in
canonical mode when those keys arrive -- on macOS it keeps the first 1024 bytes of the line and
discards the rest along with the newline, while tmux reports success. The command was left sitting
truncated at the prompt with nothing logged. The `terminal` window that the ttyd plugin configures
on every `mngr create` is 1221 bytes, so on a macOS host the web terminal never started and no
terminal URL was ever registered. Linux hosts buffer far more and were unaffected.

An additional window's command is now written to `launch_window_<n>.sh` in the agent's state dir
and sourced when it is at or above 512 bytes, the same route the agent's own command already takes.
Commands under that bound are still typed verbatim, so they stay visible in the pane, re-runnable
with up-arrow, and interpreted by the pane's own shell whatever dialect that is. A command at or
above the bound is now sourced with the POSIX `.`, so a tmux `default-command` shell without a `.`
builtin (elvish, for instance) cannot run one -- it could not have run it typed either, since it
never arrived intact.

Already-running agents keep their truncated window until they are stopped and started again;
`mngr start` returns early when the tmux session already exists.
