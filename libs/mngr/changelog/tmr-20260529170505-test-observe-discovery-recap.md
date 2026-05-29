- Fixed the `test_observe_discovery_recap` e2e tutorial test: removed the
  superfluous `@pytest.mark.modal` (read-only `mngr observe --discovery-only`
  discovers Modal via the in-process SDK, which the resource guard cannot track
  across the `mngr` subprocess boundary, so the mark tripped the guard's "never
  invoked modal" check).
- Strengthened the test to verify `mngr observe --discovery-only` actually emits
  a parseable JSONL discovery stream (a `DISCOVERY_FULL` snapshot event), rather
  than only checking that the command exits cleanly.
