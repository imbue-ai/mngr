Fixed the `test_create_with_json_output` e2e release test (tutorial `test_basic.py`):

- Scoped its verification listing to `mngr list --provider local --format json`. A bare `mngr list` queries every registered backend, and in the test environment the AWS plugin is installed without credentials, so its discovery aborted the whole listing under the default `--on-error abort`. Scoping to the local provider (where the agent is created) matches the rest of the e2e suite.

- Removed the inapplicable `@pytest.mark.rsync` marker: a local create against a git repo provisions the agent work dir with `git worktree add`, not rsync, so the resource guard correctly reported rsync was never invoked.
