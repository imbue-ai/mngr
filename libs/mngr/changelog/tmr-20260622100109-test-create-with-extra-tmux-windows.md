Fixed the `test_create_with_extra_tmux_windows` release e2e test so it reflects how a purely local-provider create actually behaves:

- The agent-created verification now scopes `mngr list` to `--provider local`. An enumerate-all `mngr list` aborts by design when any other enabled-but-unreachable backend fails discovery (e.g. a Docker daemon that isn't running, or a cloud provider plugin installed without credentials), which is unrelated to what this local test verifies.

- Dropped the `@pytest.mark.rsync` mark: a local create uses a git worktree (the `GIT_WORKTREE` transfer mode) rather than rsync, so rsync is never invoked and the resource guard correctly flagged the mark as never-invoked.
