# Mark another tmux destroy test flaky

- `test_destroy_single_agent_via_session` gains `@pytest.mark.flaky` and a 60s per-test timeout, matching its siblings in the same file: its real tmux create/destroy workload trips the global 10s pytest-timeout on a slow offload sandbox.
