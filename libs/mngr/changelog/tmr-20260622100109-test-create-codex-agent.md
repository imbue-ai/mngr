Internal (no user-facing behavior change): fixed the `test_create_codex_agent` e2e tutorial test so it runs in environments without npm, without AWS/Docker provider credentials, and with a clean source repo.

- The codex agent type installs via `npm i -g @openai/codex`; the test host has no npm, so creation failed at the install step. The create command now passes `-S agent_types.codex.check_installation=false` to skip the install check (the test verifies type resolution, not a real codex run).

- The verification listing now uses `mngr list --provider local` instead of full discovery. The codex agent is created locally, so scoping to the local provider keeps the assertion robust regardless of which remote providers are reachable (`mngr list` defaults to `--on-error abort`, so an unreachable aws/docker provider would otherwise fail the whole listing).

- Removed the superfluous `@pytest.mark.rsync`: with a clean source repo and `--no-auto-start`, worktree creation finds no uncommitted/gitignored files to transfer, so rsync is never invoked and the resource guard's superfluous-mark check failed.

Also strengthened the test's assertions: beyond the `type` label, it now confirms the codex plugin built a codex-specific launch command and that the default create produced a git-worktree work_dir.
