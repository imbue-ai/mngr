Fixed the `test_list_active_filter` e2e release test for `mngr list --active`.

- Added `@pytest.mark.timeout(60)` so the test no longer trips the default 10s per-test timeout while `mngr list` runs its full provider-discovery path (matching `test_list_local_filter`).

- The e2e test fixture now disables the credential-requiring cloud providers (aws, azure, gcp, vultr, ovh, imbue_cloud) at the user scope. Those plugins are installed and discovered by default, so an all-provider `mngr list` would otherwise exit non-zero when their credentials are absent in the test environment. Modal and Docker remain enabled so the full local/docker/modal discovery path is still exercised.
