Test-infrastructure only; no change to shipped behavior.

`cleanup_tmux_session` walks a pane's process tree with `pgrep`, and that walk had no timeout even though the neighbouring tmux and pkill calls in the same file already do. Cleanup runs inside the caller's `pytest-timeout` window, so a stuck `pgrep` stalled the test itself; one such hang failed `test_connect_start_restarts_stopped_agent` on a loaded CI run. The walk now shares a single deadline across the whole recursion -- a per-call timeout would not bound it, since it recurses once per descendant -- and returns the PIDs found so far when the budget runs out, which is what cleanup wants.

`test_connect_start_restarts_stopped_agent` and `test_stop_agent_kills_single_pane_processes` are also marked `@pytest.mark.flaky`. Both spawn a real tmux agent inside the repo-wide 10s timeout and pass locally, matching the treatment already given to their siblings in `test_destroy.py`.
