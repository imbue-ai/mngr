Fixed the `test_create_with_json_output` BASIC CREATION e2e release test so it passes reliably:

- The verification `mngr list` now scopes discovery to the local provider (`mngr list --provider local --format json`), matching the established sibling-test pattern. A plain `mngr list` enumerates every installed backend and aborts under the default `--on-error abort` whenever an unconfigured cloud provider plugin (e.g. AWS without credentials) raises during discovery.

- Removed the spurious `@pytest.mark.rsync` mark. The test creates a local worktree-based agent, whose file transfer uses a plain copy rather than the rsync binary, so the resource guard's NEVER_INVOKED check flagged the unused mark.
