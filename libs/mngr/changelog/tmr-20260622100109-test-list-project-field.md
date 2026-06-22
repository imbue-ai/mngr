Test-only: fixed the PROJECTS tutorial e2e test `test_list_project_field`.

The e2e fixture now restricts `enabled_backends` to the provider backends actually reachable in the test environment (always `local`, `modal` when credentials are present, and `docker` only when a daemon socket is reachable). Previously every registered backend stayed enabled, so an unconfigured cloud provider (e.g. `aws`) raised `ProviderUnavailableError` during discovery and aborted `mngr list` (default `--on-error abort`) with a non-zero exit even when the reachable providers produced a correct listing. This also keeps `mngr list --format json`'s `errors` array empty in sandboxes without Docker (offload/CI).

Removed the spurious `@pytest.mark.rsync` mark from `test_list_project_field`: the local command agent it creates uses a git worktree (not rsync), so the resource guard failed the test for a mark it never exercised.
