Fix and strengthen the e2e release test `test_create_with_nonexistent_base_branch`.

The test exercised a real `mngr create` (which takes ~20s, dominated by cold provider/host setup) but inherited the default 10s pytest function timeout, so pytest-timeout killed it before the command could fail. It now carries `@pytest.mark.timeout(120)`, matching the other `mngr create` tests in this module.

The test also now verifies that a failed create leaves no dangling worktree behind (via `git worktree list`), strengthening the "fail cleanly" guarantee alongside the existing branch and error-message checks.
