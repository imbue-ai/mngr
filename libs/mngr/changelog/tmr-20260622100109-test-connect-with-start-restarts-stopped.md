Fixed the `test_connect_with_start_restarts_stopped_agent` e2e release test so it reliably passes:

- Scoped the `mngr stop`/`mngr list` verification commands to the local provider (via `my-task@localhost.local` and `--provider local`) so discovery no longer fans out to unconfigured/unreachable remote providers (AWS/Vultr/...), which intermittently made each command exceed the per-command timeout.

- Reduced the number of `mngr` invocations (each pays a multi-second startup cost) and now read the restarted agent's state from `mngr list --format json`, asserting the agent left the STOPPED state. The `mngr connect --start` output ("Agent my-task is stopped, starting it") is asserted directly as proof of the restart.

- Removed the superfluous `@pytest.mark.rsync`: a local agent in a git repo syncs via a git worktree, never rsync, so the mark tripped the resource guard for an unused resource.
