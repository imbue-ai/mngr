- Fixed a flaky `test_config_set_rejects_unknown_key` e2e release test. The test
  body's first `mngr` invocation is a cold subprocess start that reliably takes
  ~20s, exceeding the 10s global pytest timeout. Added a `@pytest.mark.timeout(120)`
  override (matching the other slow tests in the same module) so cold-start latency
  no longer fails the test.
