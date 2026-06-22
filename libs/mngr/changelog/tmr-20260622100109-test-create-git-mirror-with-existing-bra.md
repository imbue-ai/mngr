Fix the `test_create_git_mirror_with_existing_branch` e2e tutorial test so it runs reliably.

The verification `mngr list` is now scoped to `--provider local` (matching the convention used by the other e2e tutorial tests): the agent runs on localhost, and querying every registered backend made `mngr list` exit non-zero whenever an unconfigured cloud provider (e.g. AWS) was enabled-but-uncredentialed in the test environment.

The test also gains an explicit `@pytest.mark.timeout(120)` (agent provisioning routinely exceeds the 10s default), and its incorrect `@pytest.mark.rsync` mark was removed -- `--transfer=git-mirror` clones the repo over git rather than rsyncing the work tree, so rsync is never invoked.
