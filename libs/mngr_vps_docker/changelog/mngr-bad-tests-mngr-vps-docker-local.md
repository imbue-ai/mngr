Test-quality cleanup of the mngr_vps_docker unit tests (no production code changed):

- `instance_test.py`: the two `_emit_docker_build_output` tests now capture log
  output and assert the BUILD-level line (stripped) is emitted for non-empty
  input and nothing is emitted for whitespace-only input, instead of only
  asserting "does not raise". The scattered `_is_retryable_rsync_error` cases
  were consolidated into a parametrized test covering one representative stderr
  string for each of the eight retryable connection patterns plus negatives.
- `_outer_helpers_test.py`: removed the duplicate `_redact_secret_env` /
  `_is_retryable_rsync_error` tests (now covered once, comprehensively, in
  `instance_test.py`) and their unused imports.
- `_snapshot_helper_test.py`: the snapshot_helper.service load test now asserts
  the resource is non-empty and contains expected systemd directives rather than
  discarding the result.
- `cloud_init_test.py`: replaced the loose bag-of-substrings generation checks
  with a single full `inline_snapshot` of the rendered user_data, so the
  load-bearing YAML indentation and key placement (the embedded SSH private key
  in particular) are pinned exactly.
- `host_store_test.py`: `test_list_persisted_agent_data_reads_all_agents_in_one_round_trip`
  now asserts the read call count does not grow with agent count (2 vs 5) rather
  than pinning a bare literal, and documents that the call-count assertion
  deliberately guards the network round-trip budget. Removed two tautological
  constructor round-trip tests.
- `config_test.py` / `primitives_test.py`: removed tautological constructor
  round-trip tests; the remaining default/wire-value contract tests carry a
  comment marking them deliberate change-detectors.
- `test_ratchets.py`: tightened the `init_methods_in_non_exception_classes`
  ratchet from 1 to 0 (the recorded count was stale; actual is 0).
