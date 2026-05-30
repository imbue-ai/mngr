Added a `@pytest.mark.timeout(120)` marker to the `test_create_headless` e2e
release test in `test_create_basic.py`, matching the per-test timeout markers
used by sibling e2e test files. Without it the test fell back to the global 10s
pytest timeout, which is too short for an e2e test that creates an agent and
runs Modal-discovery `mngr list`, causing spurious timeout failures.
