Fixed the `test_rename_dry_run_does_not_rename` e2e release test so it reliably verifies that `mngr rename --dry-run` previews a rename without applying it.

The verification `mngr list` is now scoped to `--provider local`, matching the local `command`-type agent under test, so listing no longer fails discovery when an unconfigured cloud provider (e.g. AWS) is enabled in the environment.

The incorrect `@pytest.mark.rsync` mark was removed: a local command-agent dry-run rename never invokes rsync, which tripped the resource-guard check for superfluous marks once the test body started passing.
