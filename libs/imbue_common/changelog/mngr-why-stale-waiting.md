Add a `PREVENT_BARE_TMUX_TARGETS` ratchet rule (and `check_bare_tmux_targets` helper)
that flags `tmux <subcmd> ... -t '<target>'` where the quoted target doesn't begin with
`=`. Use it from project ratchet suites (mngr does, via `rc.check_bare_tmux_targets`).

Context: bare-name tmux targets fall back to session prefix matching, which can route
commands meant for a stopped session to a still-running sibling whose name starts with
the same prefix. Routing all `-t` argument construction through the
`tmux_session_target()` / `tmux_window_target()` helpers in `imbue.mngr.hosts.tmux`
prepends `=` for exact-match resolution; this ratchet enforces that convention.
