Fixed the `mngr connect` tutorial e2e tests (`test_connect.py`). They previously
ran `mngr connect` as a plain foreground subprocess, which always failed with
"open terminal failed: not a terminal" because `tmux attach` needs a controlling
terminal. The tests now drive connect through a pseudo-terminal in the background
(new `E2eSession.connect_and_verify_attached` helper) and verify the observable
effect -- a tmux client attaches to the agent's session. Also corrected the
resource-guard marks (the local-agent connect path does not invoke the `modal`
CLI; the error-path tests create no agent so use neither tmux nor rsync) and
added a `--no-start` unhappy-path test that asserts connect refuses to start a
stopped agent.
