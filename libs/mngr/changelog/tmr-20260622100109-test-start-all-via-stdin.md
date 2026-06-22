Fixed the `test_start_all_via_stdin` e2e tutorial test (the `mngr list --ids | mngr start -` block).

The shared e2e fixture left every installed provider backend enabled, so an unscoped `mngr list` enumerated credential-only backends (aws, azure, gcp, vultr, imbue_cloud) and the docker daemon even when the test never used them. With no credentials or no daemon, discovery turned into a hard error or a hang. The fixture now pins `enabled_backends` to just the backends a test is provisioned for: `local` always, plus `modal`/`docker` only when the test carries the matching resource mark.

Also removed the superfluous `@pytest.mark.rsync` from `test_start_all_via_stdin`: it creates a local git-repo command agent, which transfers via git-worktree and never invokes rsync.
