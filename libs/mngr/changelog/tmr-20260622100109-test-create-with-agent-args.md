Fixed the `test_create_with_agent_args` e2e release test for the BASIC CREATION tutorial block.

The shared e2e fixture now restricts provider discovery to the backends each test is marked to exercise (`local` always, plus `docker`/`modal` when the test carries the matching marker). Previously the fixture left `enabled_backends` empty, so the real `mngr` subprocess discovered every provider plugin installed in the monorepo -- including credential-requiring cloud backends (aws, azure, gcp) and a possibly-absent docker daemon. Those raise `ProviderUnavailableError` during discovery, which an enumerate-all `mngr list` (default `--on-error abort`) surfaces as a hard failure, aborting unrelated tests.

Also dropped the inaccurate `@pytest.mark.rsync` from `test_create_with_agent_args`: a default (git-worktree) local create is pure git and never invokes rsync, so the mark tripped the resource guard's "marked but never invoked" check.
