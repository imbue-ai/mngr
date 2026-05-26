## mngr

- Removed misapplied `@pytest.mark.modal` from `test_create_with_connect_command` in `libs/mngr/imbue/mngr/e2e/test_create_commands.py`. The test creates a local agent and never invokes Modal in the call phase, so the mark tripped the resource guard's `NEVER_INVOKED` check (added in May 2026).
- Tightened the same test to also assert `MNGR_SESSION_NAME` and `MNGR_HOST_IS_LOCAL` reach the custom connect command, covering the full env-var contract documented in `run_connect_command`.
