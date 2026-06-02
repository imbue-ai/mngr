Fixed the imbue_cloud slow (rebuild) path. When `fast_mode=prevent` leased a host
and rebuilt its container, the rebuilt host was still marked as carrying a
pre-baked agent, so `provision_agent` took the minimal "adopt" path (which runs a
`python3` claude-config patch) against the freshly-rebuilt container -- failing
with `python3: not found`. The slow path now builds the host object with
`adopt_pre_baked_agent=False`, so `pre_baked_agent_id` is unset and mngr runs its
standard full create + provision pipeline (matching the slow path's "fresh OVH
host" contract). The rebuilt agent gets a fresh id; the bake's agent id was only
bookkeeping (release keys off the lease's host db id).

This pairs with the FCT `imbue_cloud` create template gaining the same build
config as `ovh` (`--file=Dockerfile .`, `target_path=/mngr/code/`, `fct-seed`
post-create) so the rebuild produces the FCT image rather than a bare
`debian:bookworm-slim`; those build args are ignored on the fast/adopt path.
