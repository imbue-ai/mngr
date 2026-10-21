Marked eight Modal acceptance tests `@pytest.mark.flaky`, so offload retries them instead of turning CI red on each occurrence. The 2026-10-19 flake sweep found each one recovering on a retry, on `main` or on more than one branch that does not touch the code it exercises, and none carried the marker:

- `test_snapshot_and_shutdown_missing_host_id`, `test_snapshot_and_shutdown_nonexistent_sandbox` and `test_snapshot_and_shutdown_nonexistent_host_record`: the Modal HTTP frontend answers the deployed endpoint's request itself (`404 modal-http: invalid function call`, 500, read timeouts), tracked as MIND-219.

- `test_get_and_set_host_tags`, `test_create_host_creates_sandbox_with_ssh`, `test_delete_nonexistent_snapshot_raises_error` and `test_get_host_resources`: a Modal volume or sandbox-terminate call with no deadline of its own stalls past the test's timeout (MIND-290), or a storage 503 outlasts the retry budget (MIND-477).

- `test_modal_still_reports_a_failed_build_terminating`: Modal's logs API rate-limits the build-log fetch, which does not retry it (MIND-480).

The markers are triage while those tickets are open, not fixes.
