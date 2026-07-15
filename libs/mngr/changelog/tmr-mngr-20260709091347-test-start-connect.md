Fixed the `test_start_connect` e2e tutorial test (STARTING AND STOPPING AGENTS section).

- Removed the superfluous `@pytest.mark.rsync` mark: a local command agent's work_dir is created as a same-host git worktree, so `mngr create`/`mngr start` never invoke rsync, and the resource guard rejected the unused mark.

- The test now stops `my-task` before `mngr start --connect`, so it exercises the tutorial's "start a stopped agent and immediately connect to it" path rather than starting an already-running agent.
