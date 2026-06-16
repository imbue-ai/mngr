Fixed the e2e release tests for the COMMON TASKS / MULTI-AGENT WORKFLOWS recipes.

The e2e fixture now pins `enabled_backends` to the providers these tests actually exercise (local + Modal, plus Docker when a daemon is reachable). Previously the fixture left every registered backend enabled, so the newly added credential-gated cloud backends (aws, gcp, imbue_cloud, ...) were auto-created with no usable credentials and made `mngr list` abort before reaching the agents under test.

Removed the superfluous `@pytest.mark.rsync` mark from the two recipe tests in `test_common_tasks.py`: they create local command agents in git-worktree mode, which never invoke rsync.
