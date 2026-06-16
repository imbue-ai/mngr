Made the `test_create_quiet_suppresses_output` e2e tutorial test robust:

- The post-create verification now scopes `mngr list` to the local provider (`--provider local`), where the `--type command` agent actually lives. Previously the default `--on-error abort` aborted the whole listing whenever an unrelated remote provider plugin (e.g. AWS) was installed in the environment but lacked credentials.

- Removed the stray `@pytest.mark.rsync` marker. The command agent is created in a clean git repo, so the worktree is built via `git worktree add` with no uncommitted files to transfer, meaning rsync is never invoked. The resource guard correctly flags the mark as superfluous once the test body passes.
