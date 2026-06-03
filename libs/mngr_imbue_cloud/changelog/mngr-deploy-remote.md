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

Fixed the pool-host bake writing the wrong value into `pool_hosts.vps_instance_id`:
the INSERT passed the mngr `host_id` where the OVH service name belongs, which
broke every connector-side OVH teardown (they key on `vps_instance_id`). The bake
now writes `vps_address` (the service name) via the new pure
`build_pool_host_insert_values()`, pinned by a regression test using the real
`host-`/`vps-` shapes.

`mngr imbue_cloud admin pool destroy` (and the `minds pool destroy` wrapper) now
do a full teardown: cancel the OVH VPS (strip per-lease tags + `deleteAtExpiration`)
before dropping the row, so it can no longer strand a still-billing VPS. Pass
`--skip-vps-cancel` only when the VPS is already gone. The wrapper injects the
tier's OVH credentials from Vault, like `pool create`. Relatedly, the imbue_cloud
provider's `destroy_host` now raises when the connector release fails instead of
silently cleaning up local state, so a failed release no longer makes mngr
"forget" a host whose lease/VPS is still live.

Stopped masking errors in the lease/teardown paths (error-handling audit):
- `_list_leased_hosts_cached` no longer swallows a `list_hosts` failure to an
  empty list -- a transient connector outage / expired token now propagates
  (the method already raised via `_require_account`, so callers tolerate it)
  rather than making the account look like it has zero leased hosts.
- `client.release_host` now raises `ImbueCloudConnectorError` on a transport
  error or non-2xx (e.g. the synchronous release returning 5xx because the OVH
  cancel failed) instead of returning a quiet `False`. `destroy_host` lets it
  propagate (so a failed release surfaces and local state isn't cleaned up);
  the create-rollback path (`_release_lease_quietly`) catches it explicitly to
  stay best-effort.
- The leased-host TOFU host-key scan now logs (debug) the cause when it can't
  read a remote key, so the later StrictHostKeyChecking SSH failure is
  diagnosable.
