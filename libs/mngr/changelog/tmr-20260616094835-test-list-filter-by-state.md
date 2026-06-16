Fixed the `test_list_filter_by_state` e2e release test (and hardened the shared e2e fixture it relies on).

The shared e2e fixture now pins `enabled_backends` to only the backends that are actually reachable in the test environment (local + ssh always; docker when a daemon socket is present; modal when its credentials are loaded). Previously the monorepo installs every provider plugin, so the default `mngr list` constructed a default instance for each registered backend -- including the credential-requiring cloud backends (aws, gcp) that are not configured in the test environment. Those backends deliberately raise `ProviderUnavailableError` when their credentials are missing, which aborts `mngr list` under its default `--on-error abort`; the docker backend does the same when no daemon is reachable. Restricting discovery to reachable backends makes `mngr list` deterministic regardless of which optional plugins happen to be importable.

Removed the spurious `@pytest.mark.rsync` mark from `test_list_filter_by_state`: it creates local git-repo agents, which transfer via git-worktree and never invoke rsync, so the resource guard failed with "marked with @pytest.mark.rsync but never invoked rsync".

Added a complementary `mngr list --running` assertion that the explicitly-stopped agent never appears under the running filter.
