Hardened the `test_create_headless` end-to-end tutorial test so it verifies its documented scope (a headless local agent is created, listed, and reachable) without being derailed by unrelated providers.

The listing verification now scopes discovery to the local provider (`mngr list --provider local`), matching the other local-only e2e tests. A plain `mngr list` fans out to every enabled provider, so an unreachable or uncredentialed remote provider (Docker, or an installed cloud plugin such as AWS) made the command exit non-zero even though the local agent was listed correctly.

The stale `@pytest.mark.rsync` mark was also removed: this is a purely local test and never invokes rsync (a remote-only operation), which tripped the resource guard's never-invoked check once the test began passing.
