Fix a class of bugs where tmux commands silently misroute to the wrong agent's session under prefix collision.

When `tmux ... -t name` is invoked and no session named exactly `name` exists, tmux falls back to *session-name prefix matching* and routes the command to any live session whose name starts with `name`. If two agents have names where one is a prefix of the other (e.g. `gemini` and `gemini-to-antigravity`), then when the shorter-named agent is torn down, every subsequent `-t gemini` lookup silently lands on `gemini-to-antigravity` instead of failing. Possible consequences include:

- `kill-window` / `kill-session` tearing down the wrong agent's session
- `send-keys` / `paste-buffer` delivering input to the wrong agent
- `capture-pane` reading the wrong agent's screen
- Lifecycle checks misreporting a stopped agent's state (the symptom that first surfaced this — a stopped agent shown as `WAITING` because the check landed on a live sibling's pane)
- Background-task polling loops never terminating

Changes:
- Introduce `TmuxSessionTarget` and `TmuxWindowTarget` Pydantic classes in `imbue.mngr.hosts.tmux` whose `.as_shell_arg()` renders the `-t` argument with a leading `=` (tmux's exact-match prefix), and for window/pane commands the required explicit `:window` component.
- Route every tmux `-t` call site through the helpers: lifecycle check, send-keys / paste-buffer / capture-pane in `BaseAgent`, post-attach resize script in `connect.py`, `_build_start_agent_shell_command` in `host.py`, rename / kill / has-session paths, the `listing_utils` remote-listing script, and the TUI input pipeline.
- `build_post_attach_resize_script` now iterates windows so SIGWINCH reaches every pane in every window (previously only the active window's). Side effect of the refactor; not strictly required for the prefix-matching fix.
- Update `cleanup_tmux_session` (in `utils/testing.py`) to match the new `=<session>:0` exact-match form when pkill-cleaning orphaned activity monitors — the old substring no longer appeared in the monitor's command line after the helper refactor.
- Add unit tests in `hosts/tmux_test.py` covering the helpers' rendering contract. Live behavioral coverage of the polling-loop-never-terminates failure mode lives in the per-project regression tests under `libs/mngr_claude` and `libs/mngr_gemini`.
