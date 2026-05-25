Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. The adopting test
starts at a baseline of zero violations.

Also switched `resources/ttyd_agent.sh` to use exact-session matching when attaching
to a named agent via URL arg. The previous `tmux attach -t "$_SESSION:0"` form could
silently route to a sibling session whose name starts with the requested one, e.g.
attaching by name `gemini` when `mngr-gemini` is gone but `mngr-gemini-foo` is alive
would land the browser ttyd window on the wrong agent. The script now passes
`=$_SESSION:0` so tmux refuses to misroute.
