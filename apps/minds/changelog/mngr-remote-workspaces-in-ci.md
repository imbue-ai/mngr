Release tests now exercise real remote workspace allocation against pre-baked CI pool slices (specs/remote-workspaces-in-ci.md):

- New `minds_services` tests: `test_pool_lease.py` (lease / cross-user isolation / terminal release against a real slice) and `test_pool_fast_path_create.py` (the desktop client's `mngr create` fast-path adopt of a pre-baked slice, with a live exec probe and destroy-releases-the-lease assertions).

- Empty-pool leases now FAIL by default instead of skipping (a broken bake stage must not turn the suite green); `MINDS_ALLOW_EMPTY_POOL=1` restores the skip for envs that legitimately have no pool (set automatically by `just minds-test-services-against`). `test_workspace_stop_start` adopts the same semantics.

- `DeploymentEnvsConfig` gains an optional `pool` block (`repo_branch_or_tag`, region, slice count) recorded by the bake stage so tests lease fast-path against exactly the run's own bake.

- `test_workspace_stop_start` is additionally gated behind `MINDS_STOP_START_RELEASE_TEST=1`: the measured full cycle against the standing CI box was ~2.6 hours (upload-bound at ~1.4 MB/s effective), which no CI job budget fits; the spec's open questions track raising the ci tier's upload throughput or shrinking the test artifact. Its stop-poll deadline and pytest timeout are budgeted to that measured cycle (3.5h / 4h), so an opted-in run can actually complete.

- `test_latchkey_e2e`'s in-workspace `printenv` probe now reads only the first stdout line: `mngr exec` appends a "Command succeeded on agent ..." status line to stdout, which broke the strict-equality assertion (latent since the exec status line landed; surfaced by the first release-tier dispatch in a while).

- `deployment_tests/helpers.py` gains the per-env pool-secrets Vault path (`minds/ci/runs/<env>/pool`) plus publish/delete helpers: the bake stage republishes the template repo's read-only deploy key there so the CI test job's narrower Vault role can read it.

- `test_sync_e2e`'s host-to-agent-id lookup scopes `mngr list` to the docker provider: the offload sandbox runs as root, where limactl refuses to start, so an unscoped list aborted with the provider-inaccessible exit code (latent since the sync e2e tests landed; surfaced by the first release-tier dispatch in a while).

- `ci_admin_auth_header` prefers an injected `$MINDS_ADMIN_KEY` over its Vault read (the CI test job's Vault role cannot read the tier's static supertokens entry); the relay-fleet tests skip on per-run `ci-*` envs, which by design carry no relay fleet; `test_sync_e2e`'s agent lookup uses `mngr list --on-error continue` (tolerating the provider-inaccessible exit code) instead of docker-provider scoping, and includes the list output in its failure message.

- Three more latent release-test fixes surfaced by the release-tier dispatches: `test_sync_e2e`'s host-to-agent lookup now uses the app's own `MNGR_HOST_DIR` (it pointed at the isolated config root, so the listing was always empty) with the config root as cwd, matching the sign-in helper; `test_sse_redirect` asserts the completion navigation itself (the SPA's in-app route change to `/workspace/<agent-id>`, the workspace surface) instead of waiting on a `data-ready` stamp the SPA creating page never sets; `test_litellm_via_workspace` strips `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL` from the `mngr create` env so the workspace cannot bypass the minted litellm key (the template's `pass_host_env` would otherwise forward the CI job's real key).

- `test_latchkey_e2e` carries `@pytest.mark.rsync`: the forward supervisor's state sync ships the latchkey state to the VPS via rsync, observed by the resource guard on the first CI run to reach that phase (the create transfer itself still resolves to a git push).
