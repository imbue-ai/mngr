Fixed the `test_config_set_rejects_unknown_key` scripting e2e test, which was timing out under the default 10s per-test limit because it runs multiple `mngr` subprocesses: added a `@pytest.mark.timeout(60)` override matching its sibling tests.

Extended the test to cover the full documented scope of `mngr config set` validation: it now also verifies that a wrong-type value for a known key (`config set headless notabool`) is accepted, since validation only rejects unknown fields (via `model_construct`) and does not check value types.
