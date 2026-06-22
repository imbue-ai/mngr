Fixed the `test_create_clone` tutorial e2e release test and its shared fixture:

- The e2e test environment now pins `enabled_backends` to `["local", "docker", "modal"]`. The dev venv has every mngr provider plugin installed, including credential-gated cloud backends (aws, gcp, azure, ...) that raise `ProviderUnavailableError` at construction when no credentials are configured. Under `mngr list`'s default `--on-error abort` that aborted the entire listing, so any e2e test running a bare `mngr list` failed. Pinning the backend set keeps those unconfigured backends out of discovery entirely.

- Removed the incorrect `@pytest.mark.rsync` from `test_create_clone`: `--transfer=git-mirror` transfers the repo via git, never via the rsync binary, so the rsync resource guard failed the test with a "marked but never invoked" violation.

- Gave the `mngr create` step a generous timeout (and raised the per-test timeout) so the one-time network ttyd-binary install no longer makes the create flaky against the default 30s per-command timeout.
