Fixed the `mngr rename --dry-run` e2e test (`test_rename_dry_run_does_not_rename`):

- Scoped its verification `mngr list` to the local provider so it no longer fans out to credential-requiring cloud providers (aws, azure, etc.), which abort the listing when their credentials are absent.

- Removed the superfluous `@pytest.mark.rsync` mark: a dry-run previews the rename without applying it, so it never invokes rsync, which tripped the resource-guard's superfluous-mark check.
