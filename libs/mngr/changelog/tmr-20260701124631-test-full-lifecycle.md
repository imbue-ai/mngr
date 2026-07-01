Fixed the e2e test suite to run cleanly in environments without cloud
credentials or a Docker daemon. The e2e fixture now scopes provider discovery
to the backends the test environment can actually reach (`local` and `modal`,
plus `docker` only for `@pytest.mark.docker` tests), so `mngr list` and
`mngr destroy` no longer abort with a provider-inaccessible error when the
credential-requiring cloud backends (aws, azure, gcp, ovh, vultr) or an absent
Docker daemon are enabled by default.

Also removed a superfluous `@pytest.mark.rsync` mark from
`test_full_lifecycle`; a local command agent's lifecycle never invokes rsync.
