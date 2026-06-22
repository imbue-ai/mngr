Hardened the destroy tutorial e2e tests and their shared fixture:

- The `mngr destroy --force --gc` test now allows the post-destroy garbage-collection pass enough time to finish (it queries the modal provider over the network and can exceed the previous 30s default).

- The e2e test profile now restricts `enabled_backends` to the backends these tests actually use (local, ssh, modal, plus docker when a daemon is available). Previously, with every provider plugin installed via `uv sync --all-packages`, credential-less cloud backends (aws, gcp, azure) made read commands like `mngr list` exit non-zero with a "provider not available" error, and an unreachable docker daemon did the same.

- Removed a `@pytest.mark.rsync` mark from the gc destroy test that did not match its behavior (the test uses git-based transfer for a local agent and never invokes rsync).
