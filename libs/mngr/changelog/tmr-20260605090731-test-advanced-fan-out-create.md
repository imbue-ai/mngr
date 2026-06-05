- Fix the `test_advanced_fan_out_create` e2e tutorial test so it runs. The
  fan-out loop creates four local command agents, which invokes `rsync` and
  `tmux` and takes longer than the default per-test timeout, so the test now
  carries `@pytest.mark.rsync`, `@pytest.mark.tmux`, and an extended
  `@pytest.mark.timeout(180)` (plus a larger per-command timeout for the shared
  loop). The superfluous `@pytest.mark.modal` mark was removed because the test
  substitutes local agents and never exercises Modal.
- Strengthen the same test's assertions: instead of only checking the fan-out
  loop exits 0, it now verifies that one agent was created per task (via
  `mngr list --provider local --addrs`) and that the task commands are actually
  running (via `mngr exec ... pgrep`).
