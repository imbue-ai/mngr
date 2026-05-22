Update Gemini plugin to use the structured `TmuxWindowTarget` type for tmux
pane targeting. `_send_enter_and_validate` now takes
`tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.
