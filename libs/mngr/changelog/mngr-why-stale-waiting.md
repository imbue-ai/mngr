Fix stale `WAITING` lifecycle state for stopped agents whose name is a prefix of a still-running agent.

`BaseAgent.get_lifecycle_state()` queried `tmux list-panes -t '<session>:0'` without the
`=` exact-match prefix, so when a session like `mngr-gemini` was torn down but
`mngr-gemini-to-antigravity` was still alive, tmux's default prefix matching silently
routed the query to the sibling session. The lifecycle check then read the sibling's
pane, saw a live `claude` process, and reported the stopped agent as `WAITING`.

Changes:
- Introduce `TmuxSessionTarget` and `TmuxWindowTarget` Pydantic classes in
  `imbue.mngr.hosts.tmux` whose `.as_shell_arg()` renders the `-t` argument with
  a leading `=` (and, for window/pane commands, an explicit `:window` component,
  which is required for those commands to honor `=`).
- Route every tmux `-t` call site through the helpers: lifecycle check, send-keys /
  paste-buffer / capture-pane in `BaseAgent`, post-attach resize script in `connect.py`,
  `_build_start_agent_shell_command` in `host.py`, rename / kill / has-session paths,
  the `listing_utils` remote-listing script, the TUI input pipeline, and the
  `build_tmux_capture_pane_command` builder.
- Refactor `build_post_attach_resize_script` from xargs to a per-window loop so the
  resize and SIGWINCH delivery cover every pane in every window using exact-match
  targets throughout. Previously, only the active window's panes received SIGWINCH;
  now all panes do.
- Add live prefix-collision unit tests in `hosts/tmux_test.py` that spin up two real
  sessions with overlapping names, kill the shorter one, and assert that each
  helper-built target refuses to misroute to the sibling.
- Update `cleanup_tmux_session` (in `utils/testing.py`) to match the new
  `=<session>:0` exact-match form when pkill-cleaning orphaned activity
  monitors. The old substring `list-panes -t <session>` no longer appeared in
  the monitor's command line (which now contains `list-panes -t =<session>:0`),
  so orphans were not being killed; the new pattern restores cleanup and also
  side-steps the prefix-collision the rest of this PR is about.
