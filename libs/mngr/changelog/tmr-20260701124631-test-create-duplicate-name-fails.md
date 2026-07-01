Fixed the `test_create_duplicate_name_fails` e2e release test so it verifies its intended scope reliably.

The intact-original check now scopes `mngr list` to the local provider (`--provider local --format json`) where the agent actually lives, so it no longer fails with exit code 6 when an unconfigured remote provider (e.g. AWS) is enumerated in the test environment.

Removed the spurious `@pytest.mark.rsync` mark: the test creates a local agent and never invokes rsync, which tripped the rsync resource guard.
