Fixed the `test_create_headless` e2e tutorial test and hardened the shared e2e fixture.

The e2e environment now disables the uncredentialed AWS and GCP provider backends that the monorepo dev install registers. Both backends deliberately raise `ProviderUnavailableError` when their credentials are absent, which made every `mngr list` in the e2e suite surface a spurious provider error (a non-zero exit in table format and a non-empty `errors` array in JSON) even though the agents under test are purely local.

`test_create_headless` now scopes its verification listing to `mngr list --provider local`, so it asserts on the provider the headless agent actually runs on and is no longer coupled to the reachability of unrelated remote providers. Its superfluous `@pytest.mark.rsync` marker was also removed, since a local git-repo create uses a git worktree (not rsync) to set up the agent's working directory.
