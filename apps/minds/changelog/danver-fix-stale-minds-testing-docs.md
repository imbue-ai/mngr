- Corrected stale statements in `docs/testing-overview.md`. The e2e driver list now names `materialize_paired_default_workspace_template_worktree`, the function that creates the DEFAULT_WORKSPACE_TEMPLATE worktree, in place of a function that does not exist. The Electron create-and-chat test is described as signing in through the provider chooser before it sends its first message. The Playwright section lists all four specs and says CI runs `macos-launch.spec.js` and `macos-lifecycle.spec.js`. The smoke-test section's job list includes the workflow's `resolve` job, and its local-run paragraph no longer says the suite has no CI. The e2e backlog drops the item that waited for the create UI to post to `/api/v1/workspaces`, which it already does, and renumbers the items after it.

- `test/e2e/README.md` lists `macos-lifecycle.spec.js` among the specs CI runs.

- The module docstring of `desktop_client/e2e_workspace_runner.py` no longer says the module has only two callers.
