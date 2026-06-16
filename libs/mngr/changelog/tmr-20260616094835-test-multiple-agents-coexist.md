Fixed the multi-agent release tests so they pass on hosts where default-enabled providers are unreachable.

Discovery commands (`mngr list`, `mngr exec`, etc.) fan out to every enabled provider and abort if any one of them raises, even when the agents under test live on the local provider. The e2e test profile now disables the cloud VPS providers (aws, gcp, vultr, imbue_cloud), which no e2e test exercises and which hard-error or warn when their credentials are absent, and disables the Docker provider when no Docker daemon is reachable on the host (e.g. offload sandboxes without Docker), while leaving it enabled where docker-marked tests can use it. Modal and local stay enabled.

Removed the spurious `@pytest.mark.rsync` marker from `test_multiple_agents_coexist`: the test only creates local command agents, which use a git worktree (not rsync), so the rsync resource guard correctly flagged the mark as never exercised.
