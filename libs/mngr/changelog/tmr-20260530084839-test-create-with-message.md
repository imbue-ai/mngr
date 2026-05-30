Fixed the `test_create_with_message` e2e release test for `mngr create --message`.
It now creates the agent on Modal (`--provider modal`) so the initial-message
delivery path is exercised against a real remote host, carries a 120s timeout for
the extra remote round-trips, and drops the superfluous `@pytest.mark.tmux` mark
(tmux runs inside the remote sandbox, not locally). The test also now captures the
agent's tmux pane to confirm the initial message actually landed.
