Fixed the `test_create_and_rename_agent` e2e release test so it passes in environments where extra provider plugins are installed but their backends are unreachable.

The e2e fixture now pins `enabled_backends` to the backends the environment can actually reach (local and ssh always; modal when credentials are present; docker only for `@docker`/`@docker_sdk` tests) instead of leaving it empty (which enabled every installed backend). This keeps `mngr list`/`exec`/`destroy` from failing with a provider-inaccessible exit code purely because an unconfigured cloud provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud) happens to be installed.

Also removed the incorrect `@pytest.mark.rsync` mark from the rename tests: they create local command agents in a git repo, which use the git-worktree transfer mode and never invoke rsync.
