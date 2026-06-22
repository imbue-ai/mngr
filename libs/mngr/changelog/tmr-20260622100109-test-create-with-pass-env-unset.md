Hardened the `test_create_with_pass_env_unset` tutorial e2e test (verifies that `mngr create --pass-env` silently skips a variable that is unset in the shell):

- Scoped the agent-creation check to the local provider (`mngr list --provider local`) so it no longer fails when an enabled-but-unconfigured remote provider (AWS, Azure, GCP, Docker, ...) is unreachable in the test environment.

- Gave the `mngr` commands generous per-command timeouts to absorb the CLI's interpreter-startup cost on slow (network-backed) test filesystems.

- Removed the superfluous `@pytest.mark.rsync` mark: the test creates a local command agent, whose file transfer uses a local copy rather than rsync.

- Strengthened the verification by `exec`-ing into the running agent and confirming `API_KEY` is genuinely absent from its environment (mirroring the happy-path counterpart, which confirms the forwarded value is present).
