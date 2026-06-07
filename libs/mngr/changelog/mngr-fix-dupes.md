Added per-agent tmux window sizing and resize policy.

- `mngr create` accepts `--tmux-width`, `--tmux-height`, and `--tmux-window-size` (`manual|latest|largest|smallest`). These set the agent's tmux window dimensions at session creation and its resize policy.
- Defaults are unchanged from before: a `200x50` window with tmux's default resize-on-attach behavior. `manual` pins the window to its configured size so it is never resized when a client attaches.
- The options are persisted on the agent (in `data.json`) and applied on every (re)start, so they survive `stop`/`start`, `clone`, `migrate`, and `snapshot`. They are provider-agnostic (local, docker, modal, remote).
- `mngr connect` skips its post-attach resize for a `manual`-window agent (decided on the remote host at attach time), so the pinned dimensions survive an interactive attach.
