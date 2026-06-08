- Marked two real-agent integration tests (`test_stop_agent_kills_multi_pane_processes`,
  `test_cleanup_destroy_json_output_with_real_agent`) as `@pytest.mark.flaky` so
  offload retries them. They intermittently exceed the 10s `pytest-timeout` under
  offload load while waiting on a spawned agent process; this matches the
  already-flaky sibling `test_start_restart_stopped_agent`. No production change.
