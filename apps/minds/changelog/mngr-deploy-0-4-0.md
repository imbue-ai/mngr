Deployed minds-v0.4.0 to the staging tier and recorded the deployment in the deploy history.

- Staging connector (`rsc-staging`) and LiteLLM proxy (`llm-staging`) redeployed from this branch (deploy id `20260818T165239Z`; pool-hosts migration 027 applied; RECREATE strategy; both health checks green; deployed URLs match the committed `staging/client.toml`).

- Added two staging `24sys032-us` bare-metal boxes (one per US region, matching the production box standard) and baked minds-v0.4.0 slices on each so staging leases land on the fast path.

- Added `apps/minds/docs/deploy/history/minds-v0.4.0.md` documenting the staging deployment coordinates.

- Fixed a startup race that finalized a FAILED workspace destroy as DONE: the destroy-status check treated "this host's provider has not produced a discovery snapshot yet" the same as "the host is gone", so an app restart after a failed destroy tombstoned the workspace record while the host (and its imbue_cloud lease) lived on. Finalization now requires positive evidence of gone-ness -- the host's state is DESTROYED, or the owning provider (recorded with the destroy marker) produced a clean discovery snapshot this session that omits the host. Until then the destroy stays visible as failed and retryable.

