Fixed the `test_message_one_agent` e2e tutorial test (for the `mngr message <agent>` block), which was failing for two reasons unrelated to the messaging behavior it covers.

It carried no `@pytest.mark.timeout`, so it inherited the default 10s func-only timeout and was killed mid-way through `mngr create` (agent creation legitimately takes longer than 10s). It now declares `@pytest.mark.timeout(120)`, matching its sibling agent-creating tests.

It was also marked `@pytest.mark.rsync`, but creating a local command agent in the e2e fixture's clean git repo uses git-worktree transfer mode and never invokes rsync (rsync only fires when `git status --porcelain` reports untracked/modified files). The superfluous mark tripped the resource guard's NEVER_INVOKED check once the timeout fix let the test run to completion, so the mark was removed.
