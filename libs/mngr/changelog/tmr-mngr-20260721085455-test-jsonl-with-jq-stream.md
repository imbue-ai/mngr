# Fix flaky `test_jsonl_with_jq_stream` release test

- Fixed: the `test_jsonl_with_jq_stream` output-formats release test (`mngr list --format jsonl | jq --unbuffered ...`) reliably timed out at the default 10s per-test cap. `mngr list` runs a full provider discovery that is slow when Modal credentials are present, so the pipeline routinely exceeds 10s. Added an explicit `@pytest.mark.timeout(180)`, matching the sibling read-only-Modal tests in the same file (and exceeding the command's own 120s timeout), so the test's timeout budget matches its documented behavior.
