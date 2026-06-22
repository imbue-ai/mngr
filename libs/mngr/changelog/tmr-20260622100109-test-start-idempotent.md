Fixed the `test_start_idempotent` lifecycle e2e test, which was failing because it carried `@pytest.mark.rsync` even though starting an already-running local command agent (GIT_WORKTREE transfer mode) never invokes rsync, tripping the resource guard's "marked but never invoked" check. Removed the superfluous mark.

Strengthened the same test to prove the redundant start is truly idempotent: it now records the agent's worktree path before the redundant start and asserts it is unchanged afterward, confirming the agent instance was preserved rather than torn down and recreated.
