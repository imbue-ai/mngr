Add a `@pytest.mark.timeout(60)` to `test_config_set_headless` so it doesn't trip the project-wide default 10s pytest timeout when run outside the offload `--timeout=900` wrapper.
