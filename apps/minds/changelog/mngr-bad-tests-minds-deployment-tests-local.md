Hardened the minds deployment/services test suite (`apps/minds/deployment_tests/`):

- Fixed a flakiness bug in `test_deploy_new_version`: the `/version` poller no longer aborts on a transient 5xx during the expected `DeployStrategy.RECREATE` cold-boot window; it now polls past non-200 responses like the rollback test's poller already did.
- Typed the previously-untyped `name` parameter of `_run_minds_env_destroy` in `test_deploy_round_trip` as `DevEnvName`.
- Simplified the two still-skipped skeleton tests (`test_signup_tunnel`, `test_litellm_via_workspace`) to drop their unreachable `wait_for_env_ready` preamble and use `pytest.fail` so they fail loudly if unskipped before their drivers land.
- Updated `deployment_tests/README.md`, which incorrectly claimed all tests were skipped, to reflect that four tests are now active.
