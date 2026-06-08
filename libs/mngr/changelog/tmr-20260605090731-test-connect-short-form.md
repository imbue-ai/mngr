Fixed the `test_connect_short_form` e2e tutorial test for `mngr conn`.

`mngr connect`/`conn` replaces itself with `tmux attach`, which blocks until the
user detaches and requires a real terminal, so the test could never succeed when
run headlessly (it either hung on a tty or failed with "open terminal failed:
not a terminal"). The e2e session now exposes `run_connecting_command`, which
launches the connect command under a pseudo-terminal in a background subprocess
and verifies that a client actually attaches to the agent's tmux session (the
observable effect of a successful connect). The `@pytest.mark.modal` mark was
also removed because connecting to a local agent never invokes Modal.
