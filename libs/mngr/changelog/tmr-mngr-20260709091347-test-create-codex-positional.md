Fixed the `test_create_codex_positional` tutorial e2e test so it passes on hosts without a codex binary or npm:

- The e2e fixture now sets `[agent_types.codex] check_installation = false`, so provisioning a codex agent skips the `npm i -g @openai/codex` install step (the codex tutorial tests only exercise agent-type resolution, not a real codex run).

- Dropped the spurious `@pytest.mark.rsync` from the test (local agent creation uses a git worktree, not rsync) and gave the follow-up `mngr list` a longer timeout, marking the test flaky because the just-created codex agent's live state can be slow to compute.
