Hardened the `test_create_with_message` e2e tutorial test so it no longer fails for reasons unrelated to the `mngr create --message` behavior it covers.

The verification step now scopes its listing to `mngr list --provider local` (matching the pattern already used by the other local-only e2e tests). The agent under test runs locally, so querying every registered backend was unnecessary; in particular an unconfigured cloud backend such as AWS (registered via entry point but lacking credentials in the test environment) would otherwise make `mngr list` surface a per-provider discovery error and exit 1, masking the behavior under test.

Also dropped the spurious `@pytest.mark.rsync` mark: a local create uses a git worktree (it never invokes rsync), so the resource guard's never-invoked check correctly flagged the mark once the test body started passing.
