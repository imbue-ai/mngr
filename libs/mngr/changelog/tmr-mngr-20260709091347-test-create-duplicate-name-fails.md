Fixed the `test_create_duplicate_name_fails` e2e release test so it verifies its intended scope reliably:

- Scoped the post-rejection `mngr list` to `--provider local` (matching the sibling error tests) so an unrelated unreachable remote provider (e.g. AWS without credentials) no longer makes the verification exit non-zero.

- Dropped the superfluous `@pytest.mark.rsync` mark, which the resource guard rejected because the test never invokes rsync.
