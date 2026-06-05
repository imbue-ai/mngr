Fixed the `mngr connect` e2e tutorial tests (`test_connect.py`). The happy-path
tests asserted `mngr connect my-task` succeeds, but the standalone `connect`
command execs `tmux attach` (the `connect_command` override only applies to
`create`/`start`), which aborts with "open terminal failed: not a terminal"
under the plain pipe-based test runner. Added an `E2eSession.run_connect_interactively`
helper that runs the command under a PTY, waits for the tmux client to attach,
and detaches it externally so the command exits cleanly, and switched the
happy-path tests to it (now also asserting the command attaches to the named
agent's session). Removed the superfluous `@pytest.mark.modal` from the
local-only connect tests, which never query modal (the resource guard enforces
this); the id/host error-path tests keep it.
