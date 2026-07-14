Hardened the tutorial e2e fixture so `mngr list` (and other all-provider commands) no longer aborts in environments that lack cloud credentials or a Docker daemon.

The monorepo test venv registers every provider plugin, so credential-requiring VPS backends (aws, azure, gcp, vultr, ovh, lima) were enabled by default and made `mngr list` exit non-zero during discovery. The fixture now sets `enabled_backends` to just the backends the e2e suite actually reaches: `local` and `modal` always, plus `docker` only for `@pytest.mark.docker` tests (which are the only ones that exercise the docker backend and run against a real daemon).

Also dropped the superfluous `@pytest.mark.rsync` from `test_destroy_no_gc`: it creates and destroys a local agent against a clean working tree, which transfers nothing and therefore never invokes rsync, so the mark tripped the resource guard's superfluous-mark check.
