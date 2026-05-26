Add a `PREVENT_BARE_TMUX_TARGETS` ratchet rule (and `check_bare_tmux_targets` helper)
that flags `tmux <subcmd> ... -t '<target>'` or `... -t "<target>"` where the quoted
target doesn't begin with `=`. Scans every tracked file type, not just `.py`, so
shell scripts and other non-Python tmux call sites are also covered. Use it from
project ratchet suites (mngr does, via `rc.check_bare_tmux_targets`).

Context: bare-name tmux targets fall back to session prefix matching, which can route
commands meant for a stopped session to a still-running sibling whose name starts with
the same prefix. Routing all `-t` argument construction through the
`TmuxSessionTarget` / `TmuxWindowTarget` classes in `imbue.mngr.hosts.tmux`
(via `.as_shell_arg()`) prepends `=` for exact-match resolution; this ratchet enforces
that convention.

Promote `BINARY_FILE_EXCLUSION` (a tuple of binary-file globs that would otherwise
trip `.read_text()` with `UnicodeDecodeError`) to a public `Final` constant in
`imbue.imbue_common.ratchet_testing.core` so the project ratchets and the repo-wide
meta-ratchets share one canonical list.
