Fixed: an agent's tmux pane no longer gets taken over by a message like
`'bash .../commands/sigwinch_panes.sh ... agent fit 98 43' returned 1` (the pane sits in tmux view
mode until Esc is pressed). The per-session `client-attached` / `client-resized` repaint hooks now
start `sigwinch_panes.sh` with `if-shell -b` instead of `run-shell -b`. tmux shows a failed
`run-shell -b` job's output and exit status in view mode on the most recently active pane, which is
often a different agent's than the one whose hook fired. `if-shell -b` never displays either. The
hook can fail without the script misbehaving: an older mngr checkout rewrites the shared script in
place mid-run, or the tmux server is at its file-descriptor limit, so the job's process exits 1
before the script starts. The second can happen when a burst of resize events (for example from
dragging a terminal window) fires dozens of resize hooks, each holding a server fd during its delay,
against a server running with the macOS default limit of 256. When the server has fewer than two
free fds, it cannot create the job at all; tmux then shows "Failed to run command: ..." on a
client's status line for `display-time` (750 ms by default), where it used to show nothing. tmux
also waits for an `if-shell` job before its server exits, so after `tmux kill-server` within a few
seconds of an attach or resize, the old server lingers until the script's delay ends and refuses new
sessions until then.

`sigwinch_panes.sh` now calls its entrypoint and `exit` on the same line, so a copy of the script
that an older mngr rewrites in place while a run is in progress can no longer splice other bytes into
that run. The updated script reaches a host the next time this mngr creates an agent there.

Only agents started after this change get the new hook; existing sessions keep the old one until
they are restarted, and until then a failure of an old session's hook can still take over any pane
on that tmux server, a new agent's included.
