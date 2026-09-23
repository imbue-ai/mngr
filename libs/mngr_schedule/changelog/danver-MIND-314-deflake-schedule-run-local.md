mngr_schedule: stop the `schedule run --provider local` CLI test from creating a real agent.

`test_schedule_run_local_deployed_trigger` deployed a trigger and then ran it for real. A deployed trigger's `run.sh` ends in `exec uv run mngr create --message hello`, so on CI the test started an actual agent creation, which has no bound on how long it takes. Sometimes it outlived the test's 30s timeout, and the `uv` and `mngr` processes it had started survived to the end of the pytest session, where the leak scan found them and failed the run against an unrelated test in another package. The test could not catch a regression either way: its only assertion, `assert isinstance(result.exit_code, int)`, holds for every possible outcome.

The test now puts a stub `uv` first on PATH before deploying, so the generated script execs the stub and exits at once. It asserts that the stub's distinctive exit code reaches the caller, which happens only if the creation record was found, the generated `run.sh` ran, and the CLI passed the script's status through. The `@pytest.mark.timeout(30)` that had been papering over the slow run is gone.

Test-only change; `mngr schedule run` behaves exactly as before.
