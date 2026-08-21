Release tests now exercise real remote workspace allocation against pre-baked CI pool slices (specs/remote-workspaces-in-ci.md):

- New `minds_services` tests: `test_pool_lease.py` (lease / cross-user isolation / terminal release against a real slice) and `test_pool_fast_path_create.py` (the desktop client's `mngr create` fast-path adopt of a pre-baked slice, with a live exec probe and destroy-releases-the-lease assertions).

- Empty-pool leases now FAIL by default instead of skipping (a broken bake stage must not turn the suite green); `MINDS_ALLOW_EMPTY_POOL=1` restores the skip for envs that legitimately have no pool (set automatically by `just minds-test-services-against`). `test_workspace_stop_start` adopts the same semantics.

- `DeploymentEnvsConfig` gains an optional `pool` block (`repo_branch_or_tag`, region, slice count) recorded by the bake stage so tests lease fast-path against exactly the run's own bake.

- `test_workspace_stop_start` is additionally gated behind `MINDS_STOP_START_RELEASE_TEST=1`: the measured full cycle against the standing CI box was ~2.6 hours (upload-bound at ~1.4 MB/s effective), which no CI job budget fits; the spec's open questions track raising the ci tier's upload throughput or shrinking the test artifact.
