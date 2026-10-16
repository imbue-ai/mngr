The bare-tmux-target ratchet accepts targets that are tmux ids: `$N` / `@N` / `%N` literals,
`$TMUX_PANE`, and shell variables whose name ends in `_ID`. tmux ids are unique for the object's
life and are never prefix-matched, so they need no `=` exact-match prefix.
