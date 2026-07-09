Fixed the tutorial e2e tests so `mngr list` no longer aborts on provider backends that are unreachable in the test environment.

The e2e fixture now restricts `enabled_backends` to the backends a test actually exercises: the credential-free builtins (local, ssh) plus modal/docker only for tests that opt in via `@pytest.mark.modal` / `@pytest.mark.docker`. Previously every installed provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud, ...) was loaded, and any unreachable one (e.g. aws with no credentials, or docker with no daemon) made `mngr list` exit 6 under its default `--on-error abort`, breaking tests that only touch the local provider.

Also removed the spurious `@pytest.mark.rsync` marker from `test_create_with_json_output`: creating a local `command`-type agent never invokes rsync (local agents use git worktrees, not rsync file transfer), so the resource guard correctly flagged the marker as never invoked.
