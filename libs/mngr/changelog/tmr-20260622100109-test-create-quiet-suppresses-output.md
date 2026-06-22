Fixed the `test_create_quiet_suppresses_output` e2e tutorial test so it reliably verifies a locally-created agent:

- Scoped the verification listing to `mngr list --provider local --format json`. A plain `mngr list` fans out to every registered backend, and an unconfigured cloud plugin (e.g. AWS without credentials) aborts the listing under the default `--on-error abort`, which masked the behavior under test. This matches the pattern used by the other local-create e2e tests.

- Removed the superfluous `@pytest.mark.rsync` mark. A purely local command-agent create writes its workspace directly (via a git worktree) and never invokes rsync, so the resource guard correctly flagged the mark as never-invoked.
