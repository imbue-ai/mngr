Fixed the `test_message_filtered_backend` e2e tutorial test (LABELS AND FILTERING
section). The test now creates a real backend-labeled agent and a frontend-labeled
agent before running the `mngr list --include ... --ids | mngr message -` pipeline,
and asserts the message reaches only the backend agent. Replaced the incorrect
`@pytest.mark.modal` marker (the command never invokes Modal) with the markers that
match the resources actually used (`tmux`, `rsync`) plus an explicit per-test timeout.
