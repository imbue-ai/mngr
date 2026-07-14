Fixed the `test_stop_basic` release e2e test. Its verification listings now use
`mngr list --provider local --stopped` / `--running` (scoping to the local
provider) instead of the unscoped `mngr list`, so an enabled-but-unreachable
provider (e.g. aws with no credentials) no longer makes the verification listing
exit non-zero even though the local listing is correct. Also dropped the
superfluous `@pytest.mark.rsync` mark: the test only creates a local command
agent and stops it, which never invokes rsync.
