Fix the e2e tutorial test fixture so `mngr list` no longer aborts on cloud provider backends that lack credentials.

The monorepo installs every provider plugin (mngr_aws, mngr_gcp, mngr_azure, ...), so they all registered as default backends in the e2e environment. Those credential-gated backends raise `ProviderUnavailableError` when their credentials are absent, and a full `mngr list` discovery aborts loudly on an unreachable provider by design -- so every e2e command that enumerated agents exited non-zero. The e2e profile now pins `enabled_backends` to the locally-reachable set (local/ssh/modal, plus docker only for Docker-marked tests), mirroring the surface a real PyPI user has without those plugins installed.

Also dropped the superfluous `@pytest.mark.rsync` from `test_destroy_dry_run`: it creates a local command agent and only previews/aborts a destroy, so it never transfers files to a remote host and never invokes rsync.
