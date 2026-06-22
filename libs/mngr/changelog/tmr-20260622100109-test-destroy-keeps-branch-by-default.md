Fixed the `test_destroy_keeps_branch_by_default` e2e release test so it passes reliably in environments where cloud-provider plugins (e.g. AWS) are installed without credentials.

- The destroy now uses `--no-gc`, skipping the slow, network-bound post-destroy garbage-collection pass that is orthogonal to the branch-keeping behavior under test (gc is already covered by the dedicated gc tests).

- The agent-gone check now runs `mngr list --provider local`, scoping discovery to the local provider instead of fanning out to every configured provider (a bare `mngr list` errors out when an unconfigured remote provider such as AWS cannot be reached).

- Dropped the `@pytest.mark.rsync` mark: this local git-worktree scenario never shells out to rsync, so the resource guard correctly flagged the mark as superfluous.

- Raised the per-test timeout to 120s, matching the other create+list destroy tests.
