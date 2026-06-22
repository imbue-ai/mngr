Fixed the `test_plugin_remove_rejects_invalid_name` e2e tutorial test, which timed out under the 10s default budget because every `mngr` invocation pays a ~10s cold-start cost. Added the `@pytest.mark.timeout(60)` budget used by the other subprocess-driven e2e tests in this module.

Strengthened the same test to assert that `mngr plugin remove` rejects an invalid package name with a specific exit code (1, a clean abort rather than a click usage error), names the offending input in the error, and never emits a Python traceback.
