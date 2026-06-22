Fixed the `test_destroy_specific` e2e tutorial test:

- Restricted the e2e tutorial test profile to the provider backends it can actually reach (local, modal, and docker only when a docker daemon is running). The dev monorepo installs every provider plugin, so previously all of them were loaded by default during `mngr list`; the unconfigured AWS backend raised a discovery error that made `mngr list` exit non-zero and broke destroy tutorial tests whose agents are purely local.

- Removed the spurious `@pytest.mark.rsync` from `test_destroy_specific`. The test creates a local agent inside a git repository, which always uses a git-worktree transfer (rsync transfer is rejected for git repos), so the test never invokes rsync.
