Fixed the `test_list_filter_by_state` e2e release test (and, via the shared e2e fixture, every e2e test that runs `mngr list`).

The e2e fixture left every installed provider backend enabled. Because `uv sync --all-packages` installs the credential-only cloud backends (aws, azure, gcp, ...), `mngr list` probed them, they reported themselves unreachable for lack of credentials, and the command exited with the provider-inaccessible code. The fixture now pins `enabled_backends` to `local`, `docker`, and `modal` -- the backends the test environment can actually reach -- matching the fixture's documented "Modal, Docker left enabled" intent.

Removed the stale `@pytest.mark.rsync` mark from `test_list_filter_by_state`. The test was converted from remote (Modal) agents to local `--type command` agents, which use git worktrees and never invoke rsync; the leftover mark tripped the resource guard's "marked but never invoked" check once the test began passing.
