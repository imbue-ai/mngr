Integration branch combining the user-data-layout trains (`mngr/fix-data-layout`, `mngr/declutter-template`) with `mngr/fix-apt-mirror`; the full per-train details live in this project's sibling entries for those branches.

For this project: pool and slice bakes follow the `/home/user` workspace layout and the decluttered template root (vendored mngr at `system/vendor/mngr/`, `MNGR_HOST_DIR=/home/user/.mngr`, browser env.d unit satisfied-checks instead of deferred-install markers, updated finalize/stop-services paths).

Leasing a new-layout pool host works: the fast path's host-name rewrite resolves the container's `data.json` from the provider's configured `host_dir` (falling back to the legacy `/mngr/data.json` for not-yet-rebaked pool hosts), and the LiteLLM claude-config patch derives its agent path from `host_dir` instead of the hard-coded pre-declutter `/mngr/agents/...`.
