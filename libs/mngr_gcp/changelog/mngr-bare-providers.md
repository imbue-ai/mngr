Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename: the VPS
provider is no longer Docker-only, so the package and its shape-agnostic base
classes dropped "Docker" from their names (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerHostStore` -> `VpsHostStore`, `VpsDockerError` ->
`VpsError`). Import-only change; no behavior difference.


Enabled bare placement (`isolation=NONE`): the idle agent runs `shutdown -P now`
as the VM's root, which on GCE stops the instance, so the container-only sentinel +
host-side systemd watcher is skipped for bare.

Added bare-placement (`isolation=NONE`) release tests, and fixed a resume bug they
caught: `start_host` read the host record via the Docker volume, which a bare host
does not have, so it now resolves the store through the realizer.

``stop_host`` / ``start_host`` moved to the shared base ``OfflineCapableVpsProvider``; GCP now supplies only the GCE ``_pause_cloud_instance`` / ``_resume_cloud_instance`` hooks. Behavior-preserving.

Moved mngr host identity (host id and created-at) out of GCE *labels* and into instance *metadata*, joining the host name and per-agent records already kept there. Only ``mngr-provider`` remains a label, because it is the server-side ``instances.list`` discovery filter. Host id is now stored verbatim and created-at as an ISO-8601 timestamp (no more GCE-charset lowercasing / ``%Y-%m-%dt%H-%M-%S`` encoding). Backward-incompatibility: a GCE instance created before this change carries its host id / created-at only in labels, so an *already-running* pre-upgrade host will no longer resolve by id for offline discovery / ``mngr start`` and its reconstructed created-at falls back to now(); destroy and recreate such hosts (online hosts reachable over SSH are unaffected -- they resolve via the on-volume records).

The idle-watcher install (in-container sentinel `shutdown.sh` plus the host-side systemd `.path`/`.service`) and the best-effort `_on_host_finalized` step runner moved to the shared `OfflineCapableVpsProvider`. GCP now supplies only the `GCE instance` display name; its `.service` body is the shared default `shutdown -P now` (a GCE guest poweroff stops the instance) and it does not sync host_dir to an object store, so it inherits the no-op sync gate and installs no sync daemon. The host-side idle-watcher systemd unit name changed from `mngr-gcp-idle-watcher` to the shared `mngr-idle-watcher`. Behavior-preserving otherwise.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Updated the `OfflineCapableVpsProvider` import to the new `imbue.mngr_vps.instance_offline` module (split out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

`GcpProvider` now extends the new shared `KeyValueMirrorVpsProvider`, which owns the offline read-side reconstruction over a key-value mirror (previously duplicated between GCP's metadata code and the AWS/Azure tag code). GCP supplies only the metadata-map hook (`_offline_kv_map`) and the host-name key (`_host_name_key`); the per-agent metadata *write* side (the single `setMetadata` round-trip) is unchanged, and GCP inherits no object-store/bucket machinery. The GCP-local `_agent_metadata_items` / `_agent_metadata_value` / `_persisted_agent_dicts_from_instance` / host/created-at reconstruction helpers collapse into the shared base. Behavior-preserving.

`mngr gcp prepare` / `cleanup` now resolve their `[providers.<name>]` block and refuse-on-existing-instances via the shared `mngr_vps.cli_helpers`, and `GcpProviderConfig` lifts `allowed_ssh_cidrs` into a shared config base (it keeps its own `associate_external_ip`, which GCP names differently from AWS/Azure's `associate_public_ip`) instead of carrying GCP-local copies. The cleanup refusal when instances still exist now raises the unified `ManagedResourcesExistError` (previously `GcpError`) so the message matches the other clouds. `allowed_ssh_cidrs` is now typed `ScalarStrTuple` (matching AWS) rather than a plain tuple, so a higher-precedence config layer that sets it replaces the whole list rather than being flagged as narrowing; the config key and default are unchanged.

Further internal dedup against the shared offline layer (no user-visible behavior change): `_list_provider_vps_hostnames` is now inherited from the shared `KeyValueMirrorVpsProvider` (cached listing -> non-empty `main_ip`), and `_create_vps_instance` uses the new shared `_require_parsed` helper in place of its hand-written `match`/type-narrowing guard. GCP still inherits no bucket/tag-store machinery.
