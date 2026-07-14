Added the missing `@pytest.mark.timeout(120)` to the `test_create_with_source_path` release e2e test so a real `mngr create` no longer trips the 10s default pytest timeout.

Flagged (via a FIXME in the e2e conftest) that the shared e2e fixture leaves every installed provider plugin (aws, azure, gcp, ovh, vultr, imbue_cloud, docker, ...) enabled, so `mngr list` aborts with the provider-inaccessible exit code when those providers have no credentials or daemon in the test environment.
