- `libs/mngr`: mngr now runs its agents on a private tmux server (its own
  `TMUX_TMPDIR`, under `<host_dir>/tmux`) instead of the user's default tmux
  socket. This keeps mngr's server-global tmux options and key bindings
  (`status-left-length`, the `Ctrl-q`/`Ctrl-t` destroy/stop bindings) off the
  user's own tmux sessions. The socket directory is injected centrally at the
  host command-execution layer (covering local and remote uniformly) plus the
  agent env files and the attach paths; an inherited `$TMUX` is cleared so a
  mngr process started from inside a tmux session still targets mngr's socket.
  `MNGR_TMUX_TMPDIR` overrides the directory (used by tests, and as an escape
  hatch when `host_dir` is too deep for a unix socket path).

- `libs/mngr`: **one-time upgrade note for existing users** -- agents that were
  already running on your default tmux server before this change stay there and
  now show up as `STOPPED` (mngr looks only at its private server). Reattach to a
  stranded session directly with `tmux attach -t =mngr-<agent-name>` on your
  default server to wrap up its work, then create a fresh agent. See
  `docs/tmux_users.md`.
