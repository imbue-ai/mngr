Tests only: the `snapshot_and_shutdown` endpoint's acceptance tests no longer start against an endpoint that Modal is not yet routing requests to.

The tests' fixture deployed the endpoint and then sent one warmup request, ignoring its answer. During a Modal incident on 2026-10-18 and 2026-10-19, Modal's HTTP frontend answered freshly deployed endpoints itself with `404 modal-http: invalid function call`. The warmup ignored that, so the tests failed on assertions about the endpoint's own status codes.

The fixture now waits for the endpoint itself to answer. It retries up to three times while Modal answers in the endpoint's place or nothing answers, then fails with an error naming Modal's last answer. Every status assertion also reports the response body, so a Modal-level answer is identifiable in the failure output.

`test_snapshot_and_shutdown_missing_sandbox_id` and `test_snapshot_and_shutdown_nonexistent_host_record` are no longer marked `@pytest.mark.flaky`. In the last 14 days of CI they flaked only during that Modal incident. The other three tests in the module keep their marks: each also failed with a Modal frontend answer outside the incident (`_success` on 2026-10-12, `_nonexistent_sandbox` on 2026-10-14 and 2026-10-21, `_missing_host_id` on 2026-10-22).
