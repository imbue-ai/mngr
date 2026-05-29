Removed the incorrect `@pytest.mark.modal` mark from the `test_stop_all_via_stdin`
e2e release test. The test creates a local `--type command` agent and never invokes
the Modal CLI (Modal is only tracked when the `modal` binary runs, e.g. via
`environment_create`/`deploy` during `--provider modal` creation), so the mark
triggered a spurious "marked with @pytest.mark.modal but never invoked modal"
resource-guard failure. The test still exercises tmux and rsync, which remain marked.

Also strengthened `test_stop_all_via_stdin` to create multiple agents and assert each
one actually transitions to `STOPPED` (rather than only checking the command exit
code), and added `test_stop_all_via_stdin_with_no_agents` covering the edge case where
the piped id list is empty (`mngr stop -` on empty stdin must succeed as a no-op).
