- Fixed the `test_create_with_project_label` e2e release test. Its verification
  `mngr list` now scopes discovery to the local provider (`--provider local`),
  so an unconfigured remote backend installed in the dev venv (e.g. the aws
  plugin with no credentials) can no longer abort the listing and fail the
  test for reasons unrelated to the project label being checked. Also removed
  the test's incorrect `@pytest.mark.rsync` mark: a local git-worktree create
  does not invoke rsync.

- Strengthened the same test to verify that the project label is usable for
  filtering: `mngr list --project my-project` selects the agent, while a
  different project filter does not.
