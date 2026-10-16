`capture_tmux_pane_content` and `build_tmux_capture_pane_command` take `include_styles`,
which captures with `tmux capture-pane -e` so each cell's colour, dim and reverse-video
attributes come back as escape sequences. `BaseAgent._capture_pane_content` passes it through.

Interactive TUI agents gain a `_capture_input_box_pane_content` hook, through which the
pre-paste check for leftover input-box text reads the pane. It defaults to the plain capture;
the claude agent overrides it to read its input box without the hints Claude draws there.
