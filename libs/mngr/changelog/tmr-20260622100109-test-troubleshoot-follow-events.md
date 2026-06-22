Removed the superfluous `@pytest.mark.rsync` mark from the `test_troubleshoot_follow_events` e2e tutorial test. The test creates a local command agent backed by a git repository, which mngr provisions via a git worktree (not rsync), so the mark tripped the resource guard's superfluous-mark check.

Strengthened the same test to also assert that the `mngr event --follow` output is well-formed JSONL, not just that the streamed command runs until `timeout` kills it.
