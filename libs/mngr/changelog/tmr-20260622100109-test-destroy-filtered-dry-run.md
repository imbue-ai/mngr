Stabilized the `test_destroy_filtered_dry_run` LABELS tutorial e2e test.

- Added a `@pytest.mark.timeout(180)` to the test (it previously inherited the
  10s default and could not finish even the `mngr create` setup step) and gave
  the unscoped tutorial `mngr list | mngr destroy --dry-run` pipeline a generous
  per-command timeout, since discovering every enabled provider can be slow in
  the e2e environment.

- Scoped the setup/verification `mngr list` calls to `--provider local` so they
  observe the local agent deterministically instead of exiting non-zero when an
  uncredentialed cloud provider (e.g. AWS) is enabled in the e2e environment.

- Removed the now-inaccurate `@pytest.mark.rsync` mark: the test only creates a
  local agent and dry-run-destroys it, so it never invokes rsync.

- Strengthened the dry-run assertion to check for the conditional "Would
  destroy" preview phrasing, which distinguishes a dry-run from a real destroy.
