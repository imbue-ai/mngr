- `libs/mngr`: agent tmux sessions now apply the mngr host tmux config even
  when a tmux server is already running. Previously the config was passed only
  via `tmux -f <config> new-session`, which tmux honors solely when it *starts*
  a new server; any session created on an already-running server (the common
  case once one agent is up) silently inherited tmux defaults. That dropped the
  widened `status-left-length` (so `[mngr-<agent>]` was clipped to 10 chars) and
  the `Ctrl-q` / `Ctrl-t` destroy/stop hotkeys. Session creation now runs
  `tmux source-file <config>` right after `new-session`, so these apply
  regardless of server state.

- `libs/mngr`: the host tmux config now enables `set-titles` (`set -g
  set-titles on` with `set-titles-string "#S  #T"`), so the agent's session
  name and pane title are forwarded to the outer terminal's tab (e.g. the
  iTerm2 tab title) instead of falling back to `<profile>(tmux)`.

- `libs/mngr`: mngr's generated `~/.mngr/tmux.conf` no longer sources the
  user's `~/.tmux.conf`, and the agent's tmux server is no longer started with
  `-f` pointing at the mngr config. tmux loads `~/.tmux.conf` itself, once, when
  the server starts; mngr's config (sourced at agent creation) now contains only
  mngr's own settings. Re-sourcing `~/.tmux.conf` on every agent creation could
  re-run non-idempotent user config (e.g. `set -ag`, plugin `run-shell`) and
  corrupt the user's setup.
