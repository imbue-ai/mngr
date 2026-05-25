Hardened the workspace-restart shell command in `desktop_client/app.py` to use
exact-session matching. The previous `tmux kill-window -t "${MNGR_PREFIX}system-services:svc-system_interface"`
form had no leading `=`, so if the `${MNGR_PREFIX}system-services` session was gone
but a sibling-prefix session was alive, the kill-window could silently land on the
wrong agent's session and kill a window there. The command now uses
`-t "=${MNGR_PREFIX}system-services:svc-system_interface"` so tmux refuses to misroute.

To prevent recurrences, adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule
(added in `imbue_common`) via `rc.check_bare_tmux_targets(_DIR, snapshot(0))` in
this project's `test_ratchets.py`. The ratchet flags new occurrences of
`tmux <subcmd> -t '<bare-name>'` -- targets without a leading `=` exact-match
prefix, which can silently route commands to a sibling session whose name shares
a prefix with the intended one. The adopting test starts at a baseline of zero
violations.
