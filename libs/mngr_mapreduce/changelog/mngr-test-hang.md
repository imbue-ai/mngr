`stop_agent_on_host` now also tolerates the `CleanupFailedGroup` that `Host.stop_agents`
raises when cleanup leaves a resource behind, so a best-effort stop in a `finally` logs and
continues instead of masking the real result.

`test_run_mngr_raw_returns_finished_process` no longer races the global 10s pytest timeout
against its own 10s subprocess budget: the test function now gets a 30s timeout so a slow
cold `mngr` start under load no longer flakes it.
