Fixed the `test_destroy_multiple_at_once` e2e tutorial test (and hardened the shared e2e fixture):

- The e2e tutorial test fixture now restricts provider discovery (via `enabled_backends`) to the backends each test can actually reach: the `local` backend is always enabled, while `docker` and `modal` are enabled only for tests carrying the matching marker. Previously every installed provider plugin was left enabled, so a credential-requiring cloud backend (e.g. `aws`) or an unreachable Docker daemon (on macOS / non-docker CI runners) raised `ProviderUnavailableError` during discovery and aborted `mngr list` under the default `--on-error abort`, breaking e2e tests that verify state via `mngr list`.

- Removed the spurious `@pytest.mark.rsync` marker from `test_destroy_multiple_at_once`: the test creates git-repo agents on the same host, which use the `git-worktree` transfer (never rsync), so the resource guard flagged the unused marker once the test could run to completion.

- Added `test_destroy_multiple_aborts_on_unknown_agent`, an unhappy-path companion for the "destroy multiple agents at once" tutorial block: it verifies that a forced multi-target destroy containing one unmatched name reports the error and destroys nothing (all-or-nothing), leaving the real agents intact.
