Update the Antigravity plugin to use the structured `TmuxWindowTarget` type for
tmux pane targeting. `_send_enter_and_validate` now takes
`tmux_target: TmuxWindowTarget` instead of a bare string, matching the
`BaseAgent` API change in `libs/mngr` that fixes stale `WAITING` lifecycle
state caused by tmux session-name prefix matching.

Fix `antigravity_background_tasks.sh` to use the `=` exact-match prefix in its
`tmux has-session` polling loop. Without `=`, the loop would never exit when an
Antigravity agent's session was killed but a sibling session whose name shares
this name as a prefix was still alive, leaking the transcript streamer and
common-transcript converter for stopped agents.
