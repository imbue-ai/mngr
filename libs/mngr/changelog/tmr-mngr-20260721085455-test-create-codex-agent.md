Fixed the `test_create_codex_agent` e2e tutorial test so it exercises codex agent-type resolution on a host without a codex binary or npm:

- The test now disables the codex install check (`mngr config set --scope local agent_types.codex.check_installation false`) before creating the agent, so provisioning no longer tries to `npm i -g @openai/codex` and fail.

- Removed the spurious `@pytest.mark.rsync` mark: the test creates a worktree agent from a clean source repo, so `_transfer_extra_files` never shells out to rsync, and the mark caused a NEVER_INVOKED resource-guard failure.
