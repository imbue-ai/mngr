Fixed the `test_create_with_base_branch` e2e tutorial test so it passes when run directly (not just under offload, which overrides the per-test timeout):

- Added `@pytest.mark.timeout(120)`, matching the sibling `mngr create` tutorial tests. Without it the test inherited the 10s default and timed out during `mngr create` (which alone takes ~30s).

- Removed the incorrect `@pytest.mark.rsync` mark. The tutorial command creates a local git agent via the default git-worktree transfer, which never invokes rsync for a clean repo, so the resource guard failed the test ("marked rsync but never invoked rsync").
