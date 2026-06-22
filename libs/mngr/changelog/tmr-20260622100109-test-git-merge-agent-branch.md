Fixed the `test_git_merge_agent_branch` e2e tutorial test, which was failing for two reasons:

- It lacked an explicit `@pytest.mark.timeout`, so the global 10s default fired during `mngr create` before the agent finished starting. Added `@pytest.mark.timeout(120)` to match the other agent-creating tests in the module.

- It carried `@pytest.mark.rsync`, but a local agent in a git repo uses a git worktree (not rsync) for its transfer, so the rsync resource guard reported the mark as never invoked. Removed the spurious mark.

Also strengthened the coverage of the `git merge mngr/<agent>` tutorial line: `test_git_merge_agent_branch` now asserts the fast-forward path explicitly, and a new companion test `test_git_merge_agent_branch_creates_merge_commit` covers the non-fast-forward path, where the caller has committed diverging work locally and `git merge` produces a real merge commit (verified via two parents) that combines both the agent's and the caller's changes.
