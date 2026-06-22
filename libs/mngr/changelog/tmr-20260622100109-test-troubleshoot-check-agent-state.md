Fixed the troubleshooting tutorial e2e tests so full-discovery commands like `mngr list` no longer fail in CI:

- The e2e fixture now pins `enabled_backends` to the backends a test actually provisions (always `local`/`ssh`, plus `docker`/`modal` only when the test carries the matching marker). Previously every provider plugin installed in the monorepo (aws, gcp, azure, vultr, ovh, ...) was registered and queried; after unauthenticated/unreachable providers began raising instead of silently reporting empty, this made `mngr list` exit non-zero even when the target agent was discovered correctly.

- Removed the superfluous `@pytest.mark.rsync` from `test_troubleshoot_check_agent_state`; it creates a local `command` agent and runs `mngr list`, neither of which invokes rsync, so the resource guard flagged the unused mark.
