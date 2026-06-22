Fixed the e2e tutorial test `test_stop_archive`:

- Removed the spurious `@pytest.mark.rsync` mark. That test creates a local command agent with `--no-connect` and only stops/archives and lists it, so no remote source transfer and no rsync-backed connect/attach ever happens -- the rsync binary is never invoked, and the resource guard correctly flagged the mark as superfluous.

- Hardened it against an observed flake where the local tmux-backed `mngr stop --archive` occasionally exceeded the 30s per-command default on a slow sandbox: the stop call now gets a 60s timeout and the test is marked `@pytest.mark.flaky` so offload retries it if it slips further (matching the precedent on `test_destroy_single_agent`).
