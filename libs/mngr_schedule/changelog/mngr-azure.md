## Test reliability

- Marked `test_schedule_run_local_deployed_trigger` with `@pytest.mark.flaky` so offload retries it. It passes locally (~5s) but occasionally exceeds the 10s pytest-timeout under offload load. This is unrelated to the Azure-provider work on this branch -- it just surfaced on this PR's CI run.
