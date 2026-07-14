Tests: fixed the e2e tutorial test for the `--type command` post-`--` command path so it verifies its documented scope. Two test-infrastructure fixes:

- The shared e2e fixture now restricts provider discovery to the backends each test actually exercises (`local` always, `modal` always, and `docker` only for `@pytest.mark.docker` tests) via `enabled_backends`. Previously `uv sync --all-packages` installed every provider plugin (aws, azure, gcp, vultr, ovh, imbue_cloud) into the dev venv, so a bare `mngr list` fanned out to all of them and aborted with the provider-inaccessible exit code because those cloud providers have no credentials in the test environment.

- Dropped the spurious `@pytest.mark.rsync` from `test_create_command_agent_runs_post_dash_command_in_agent`: it creates a local agent in a git repo, which uses the `GIT_WORKTREE` transfer mode and never shells out to rsync, so the declared-but-never-invoked resource guard failed the otherwise-passing test.
