Fixed the e2e tutorial test suite so `mngr list` no longer fails on credential-backed cloud providers.

The shared e2e fixture now disables the credential-backed cloud VPS providers (aws, azure, gcp, vultr, ovh) that are enabled by default in the all-packages dev/CI environment. Without credentials these providers report themselves unreachable, and a plain `mngr list` treats an enabled-but-unreachable provider as an error and exits with the provider-inaccessible code (6), which broke every tutorial test that lists agents. Modal and Docker stay enabled as before.

Also dropped the superfluous `@pytest.mark.rsync` from `test_create_short_forms`: both agents use the default git-worktree transfer in a clean source repo, so `mngr create` finds no untracked files to copy and never invokes rsync, which the resource guard flags as a superfluous mark.
