Removed the superfluous `@pytest.mark.modal` from the e2e tutorial test
`test_create_with_extra_tmux_windows`. That test exercises only the local
provider (it verifies an extra tmux window on the local tmux server), so the
modal resource guard correctly flagged it as marked-but-never-invoked: the only
incidental modal interaction (`mngr list` discovery) happens in the mngr
subprocess via the SDK, which the in-process guard cannot observe. Removing the
mark fixes the test while keeping it in the `release` lane.

Also strengthened the test to match the tutorial more faithfully: it now creates
two named extra windows ("server" and "logs", as the tutorial shows) instead of
one, asserts both windows exist, and verifies via `mngr exec ... ps aux` that
each window's command is actually running. The per-test timeout is raised to 30s
to accommodate the extra `mngr exec` command.
