Strengthened the `test_create_default_branch` e2e test to verify that creating
an agent leaves the host repository on its original branch (the agent's branch
lives in an isolated worktree), confirming the "changes don't conflict" promise.
