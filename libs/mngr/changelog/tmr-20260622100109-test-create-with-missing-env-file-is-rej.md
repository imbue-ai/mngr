Give the `test_create_with_missing_env_file_is_rejected` e2e tutorial test a 120s timeout override.

The test creates an agent (rejected up front because `--env-file` points at a nonexistent file) and then runs `mngr list` to confirm no agent was created. Together these exceed the default 10s per-test timeout, so the test was hitting a spurious pytest-timeout failure on the `mngr list` step. The override mirrors the sibling `test_control_mngr_via_env`, which already raises the timeout for the same create-plus-list reason.
