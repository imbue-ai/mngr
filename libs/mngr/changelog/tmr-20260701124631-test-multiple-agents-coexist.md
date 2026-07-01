Fixed the `test_multiple_agents_coexist` e2e release test so it reflects the intended local-agent scope.

The test now scopes its listing assertion to `mngr list --provider local` (matching the sibling e2e tests): a bare `mngr list` also queries every other enabled provider, and an enabled-but-unauthenticated cloud provider (e.g. AWS with no credentials in CI) makes the command exit non-zero even though the local agents listed correctly. This is intentional `mngr list` behavior for unreachable providers, so the test now targets only the provider under test.

Also removed the superfluous `@pytest.mark.rsync` mark: the test creates only local git-worktree agents, which never invoke rsync (rsync is used only for local-to-remote file transfer), so the resource guard correctly flagged the mark as never exercised.
