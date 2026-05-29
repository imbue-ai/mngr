Fixed the `test_advanced_fan_out_create` e2e tutorial test so its pytest markers
match what the test actually exercises: added `@pytest.mark.rsync`,
`@pytest.mark.tmux`, and `@pytest.mark.timeout(180)` (creating four local command
agents in a loop uses rsync/tmux and exceeds the default 10s timeout), and removed
the unused `@pytest.mark.modal` mark (the substituted local-command fan-out never
invokes Modal).

Also strengthened the test to verify the fan-out's concrete effects rather than
just the loop's exit code: it now parses `mngr list --format json` and asserts
that all four task-named agents exist, are alive command agents running the
expected command, and each landed in its own distinct worktree.
