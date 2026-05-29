- Fixed the `test_create_headless` e2e tutorial test: removed the superfluous
  `@pytest.mark.modal` mark. The test only creates a local agent and runs `mngr
  list`, neither of which invokes the `modal` CLI binary (the only modal usage
  the resource guard can track across the `mngr` subprocess boundary), so the
  mark tripped the guard's "marked with @pytest.mark.modal but never invoked
  modal" check.
- Strengthened the same test to verify the headless agent is genuinely running
  and reachable via `mngr exec my-task pwd`, not merely present in `mngr list`,
  and bumped its per-test timeout to 30s to accommodate the extra probe.
