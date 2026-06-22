Fixed the `test_create_with_label` e2e tutorial test (no user-visible product
change). Its verification `mngr list` is now scoped to `--provider local` (the
provider the agent is created on) so an unconfigured cloud plugin that happens to
be installed -- e.g. the AWS backend with no credentials -- cannot abort the
listing with an unrelated discovery error. Also removed the test's
`@pytest.mark.rsync` mark: the create runs against a git repo and transfers via
`git worktree add`, never rsync, so the resource guard's NEVER_INVOKED check
flagged the mark as superfluous.
