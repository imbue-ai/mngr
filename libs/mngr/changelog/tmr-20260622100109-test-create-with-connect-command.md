Fixed the `test_create_with_connect_command` release test (custom `--connect-command` tutorial block).

The verification step now scopes its `mngr list` to `--provider local`, where the default-provider agent actually runs. In a full-workspace dev/e2e environment every provider backend plugin is installed, so an unscoped `mngr list` (which defaults to `--on-error abort`) aborted when a credential-requiring cloud backend such as `aws` was reached without credentials. Scoping to the local provider keeps the verification focused on where the agent lives and matches what a default PyPI user (who only installs the backends they use) experiences.

Also removed the test's incorrect `@pytest.mark.rsync`: the e2e working directory is a git repo, so create uses a git-worktree (not an rsync copy) and local file operations write directly without shelling out to rsync, so rsync is never invoked. The test still asserts the agent's command and type, not just its name.
