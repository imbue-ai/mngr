Tests: the git tutorial e2e test `test_list_fields_original_branch_with_agent` now passes in environments without cloud credentials or a Docker daemon.

The shared e2e fixture now disables the cloud providers that require credentials absent from the e2e environment (AWS, Azure, GCP, Vultr, OVH, imbue_cloud), and only enables Docker for tests marked `@pytest.mark.docker`. Previously any discovery-triggering command (e.g. `mngr list`) would exit non-zero (`EXIT_CODE_PROVIDER_INACCESSIBLE`) when one of these providers was enabled but unreachable, failing tests that had produced correct output.

The `@pytest.mark.rsync` mark was removed from `test_list_fields_original_branch_with_agent`, which creates a local command agent and lists it -- it never invokes rsync, so the mark was superfluous and tripped the resource guard.
