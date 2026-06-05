Fixed the `test_connect_no_start` e2e tutorial test. It now allows enough time for
the create+connect flow (adds `@pytest.mark.timeout(180)`), drops the superfluous
`@pytest.mark.modal` mark (the local create/connect path never invokes Modal), and
asserts the real behavior of `mngr connect my-task --no-start`: a running agent is
accepted (no auto-start-disabled error) and connect proceeds to a real `tmux attach`,
which fails cleanly with "open terminal failed" in the headless harness. Also
corrected the module docstring, which incorrectly claimed the e2e fixture rewrites
the standalone `connect` command to a no-op recorder.
