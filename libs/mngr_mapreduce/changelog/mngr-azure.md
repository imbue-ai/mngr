## Tests

- Marked `test_run_mngr_raw_returns_finished_process` as `@pytest.mark.flaky`: its `mngr config list` subprocess has a hard 10s budget, and a cold `mngr` start under heavy offload parallelism can occasionally exceed it. Offload now retries it automatically.
