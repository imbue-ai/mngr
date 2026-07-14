Fixed the `test_destroy_specific` e2e tutorial test and its shared fixture so it runs correctly in environments without cloud credentials or a Docker daemon:

- The e2e fixture now disables the credential-requiring cloud VPS provider backends (aws, azure, gcp, ovh, vultr) that the monorepo venv installs but the test environment has no credentials for. Previously they made `mngr list` exit non-zero with `EXIT_CODE_PROVIDER_INACCESSIBLE` even when the listing itself succeeded.

- The e2e fixture now enables the Docker provider only when a Docker daemon is actually reachable, so non-docker tests still pass in environments without one (while the release offload image, which starts dockerd, keeps Docker enabled).

- Removed the incorrect `@pytest.mark.rsync` mark from `test_destroy_specific`: it creates a local command agent from a clean git repo, which transfers via git-worktree and never invokes rsync.

- Strengthened the test to assert the confirmation prompt ("Are you sure you want to continue?") is shown, so a regression that destroyed a specific agent without prompting would be caught.
