Renamed the `_AGENT_TAG_FIELDS` constant imported from `mngr_vps` to the
public `AGENT_TAG_FIELDS` (matching its sibling `AGENT_TAG_PREFIX`), so the
Azure tag-mirror code no longer imports a private name across modules. No
behavior change.


Updated imports for the `mngr_vps_docker` -> `mngr_vps` package rename and the
accompanying class renames (`VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `VpsDockerHostRecord` ->
`VpsHostRecord`, `VpsDockerError` -> `VpsError`, etc.). Import-only; no behavior
change.


Enabled bare placement (`isolation=NONE`): an Azure OS shutdown does not halt
compute billing, so the bare agent's idle `shutdown.sh` runs the ARM
self-deallocate directly (the same call the container idle watcher uses), keeping
the self-deallocate role assignment and skipping the host-side sentinel watcher.

Added bare-placement (`isolation=NONE`) release tests, and fixed a resume bug they
caught: `start_host` read the host record via the Docker volume, which a bare host
does not have, so it now resolves the store through the realizer.

Bugfix: `mngr start` of a deallocated Azure host now re-mirrors the resumed host
record to the external (Blob bucket) store, so offline / `mngr list` reads no
longer report a just-resumed Azure VM as STOPPED until the next mirroring write.

``stop_host`` / ``start_host`` moved to the shared base ``OfflineCapableVpsProvider``; Azure now supplies only the deallocate/start hooks plus the static-IP known_hosts rebind no-ops. The shared base is what now guarantees the resume-mirror above happens on every provider.

Updated the host_dir sync to call the realizer's `host_dir_path_on_outer`
directly after the redundant `_host_dir_path_on_outer` forwarder was removed
from the shared VPS provider. No behavior change.

The idle-watcher install, the host_dir-to-bucket sync daemon install/before-pause, and the best-effort `_on_host_finalized` step runner all moved to the shared `OfflineCapableVpsProvider`. Azure now supplies only its hooks: the `Azure VM` display name; the ARM self-deallocate `.service` body and the curl-plus-deallocate-script outer prep (`_idle_watcher_service_unit` / `_prepare_idle_watcher_outer`); the bare-placement self-deallocate `shutdown.sh` (`_write_bare_idle_shutdown_script`); the sync gate (config flag + bucket + managed identity present), azcopy install, `azcopy sync` `.service` body, and blob target URL; and the self-deallocate role assignment prepended via `_post_finalize_steps`. The host-side systemd unit names changed from `mngr-azure-idle-watcher` / `mngr-azure-host-dir-sync` to the shared `mngr-idle-watcher` / `mngr-host-dir-sync`. Behavior-preserving otherwise.

Updated the VPS build-arg parsing imports to point at the new `imbue.mngr_vps.build_args` module (moved out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

Updated imports for `TagMirrorVpsProvider`, `AGENT_TAG_PREFIX`, `AGENT_TAG_FIELDS`, and the host_dir-sync unit symbols to the new `imbue.mngr_vps.instance_offline` module (split out of `imbue.mngr_vps.instance`). Import-only change; no behavior difference.

The shared offline read-side reconstruction moved up into the new `KeyValueMirrorVpsProvider` base that `TagMirrorVpsProvider` now extends, so the Azure provider's host-name hook was renamed `_host_name_tag_key` -> `_host_name_key` and its tag-mirror agent-record write call now invokes the renamed `_agent_field_items` (formerly `_agent_field_tags`). The 256-char tag-value cap is still applied (the base reads it from the new `_max_value_len` hook). Internal refactor; no user-visible behavior change.

The host_dir-sync daemon now runs its `azcopy sync` command from an installed `/usr/local/sbin/mngr-host-dir-sync.sh` script (referenced directly by the oneshot `.service`'s `ExecStart`) instead of an inline `ExecStart=/bin/sh -c '...'`, removing a layer of systemd + shell quoting around the host_dir path and blob URL; the MSI `Environment=` lines stay on the unit. The sync and self-deallocate `.service` units are now rendered via the shared `render_systemd_unit` helper. No behavior change.

`BlobVolume` is now a thin subclass of the shared `BaseObjectStoreVolume` (in
`mngr_vps.state_bucket_base`), supplying only the Azure Blob primitives and an
error seam (`_translate_errors` / `_is_not_found` / `_bucket_error_type`); the
listing / existence / read / write / delete logic it duplicated with the AWS
`S3Volume` now lives on the base. `BlobStateBucket`'s `_get_object` /
`_delete_object` / `_prefix_has_objects` likewise moved to `BaseStateBucket`,
leaving the bucket with just its raw Blob primitives and the seam. The
one-at-a-time blob delete (Blob storage has no batch delete) stays Azure-specific.
No user-visible behavior change.

`mngr azure prepare` / `cleanup` now resolve their `[providers.<name>]` block and refuse-on-existing-VMs via the shared `mngr_vps.cli_helpers`, and `AzureProviderConfig` lifts `allowed_ssh_cidrs` / `associate_public_ip` into shared config bases instead of carrying Azure-local copies. The cleanup refusal when VMs still exist now raises the unified `ManagedResourcesExistError` (previously `AzureProviderError`) so the message matches the other clouds. `allowed_ssh_cidrs` is now typed `ScalarStrTuple` (matching AWS) rather than a plain tuple, so a higher-precedence config layer that sets it replaces the whole list rather than being flagged as narrowing; the config key and default are unchanged.

Further internal dedup against the shared offline layer (no user-visible behavior change): the Azure VM-tag `HostStateStore` (`_VmTagHostStateStore`) is gone in favor of the shared `TagHostStateStore`, with Azure supplying only a `_remove_instance_tags` hook; the `_state_store` selection now comes from the base via new `_bucket_error_type` / `_bucket_label` hooks (`_state_bucket` is unchanged); the host_dir-sync daemon install now returns a `HostDirSyncInstallPlan` (returning `None` to skip when the bucket-write managed identity is absent, preserving the prior skip behavior) consumed by the shared `BucketHostDirBackend.install_sync` skeleton; `_list_provider_vps_hostnames` is inherited from the shared base (the deallocated-VM-keeps-its-Static-IP rationale moved to that base method); and `_create_vps_instance` uses the shared `_require_parsed` helper.

Localized cleanup (no user-visible behavior change): the bare-placement `_write_bare_idle_shutdown_script` now writes its ARM self-deallocate `shutdown.sh` via the shared `VpsProvider._write_shutdown_script` plumbing instead of repeating the mkdir/write/chmod sequence.

Integrated the `mngr/volumes` offline-store simplification (commit `f8bb5c0a5`): the per-agent instance-tag mirror is removed in favor of a single uniform external `HostStateStore` per provider -- AWS/Azure use their object-storage state bucket as the sole offline store (a stopped host's offline metadata now requires the bucket; the provider's `_state_store` raises an actionable `missing_state_bucket_error` pointing at `mngr <cloud> prepare` when the bucket is absent), and GCP uses a lossless instance-metadata-backed store (full host record + one JSON value per agent). AWS/Azure/GCP now extend `OfflineCapableVpsProvider` directly. This supersedes the earlier-on-this-branch tag-mirror dedup (the lifted `TagHostStateStore` / `KeyValueMirrorVpsProvider` / `TagMirrorVpsProvider` are gone); the realizer architecture, the systemd-unit hardening, and the cli/config/state-bucket dedup are retained. No behavior change for container hosts beyond the offline-metadata-requires-bucket consequence noted above.

Bugfix: a running bare (`isolation=NONE`) host is now discoverable and reachable
with the default provider config -- `mngr conn`/`list`/`stop`/`start`/`destroy`
no longer need `-S providers.<name>.isolation=NONE` at connect time. Instances
now carry a `mngr-isolation` tag stamped at create (alongside `mngr-host-id` /
`mngr-provider`), so discovery reads the host's placement from the cloud API
without SSH and probes it with the matching realizer. Pre-existing hosts have no
tag and default to container, preserving prior behavior.

Behavior-preserving dedup against the shared offline layer. The Azure `_state_store` / `_host_dir_backend` cached properties are now thin wrappers over the shared `OfflineCapableVpsProvider._select_bucket_store` / `_select_bucket_host_dir_backend` (supplying only the resolved Blob bucket, its label, and `mngr azure prepare`). The near-identical `_offline_discovered_host_from_instance` is dropped in favor of the shared default; Azure now sets only the `mngr-host-name` host-name tag key via the new `_host_name_tag_key()` hook. No user-visible behavior change.


Bugfix: `rename_host` now re-stamps the cheap `mngr-host-name` VM tag that offline
discovery reads (previously stamped only at create), so a host renamed and then
stopped lists under its new name rather than its old one. The re-stamp merges into
the VM's existing tags rather than replacing them, preserving the other index tags
(`mngr-host-id`, `mngr-provider`, etc.).

Doc: removed a stale README note about speculative `create_snapshot` /
`list_snapshots` / `delete_snapshot` client methods that no longer exist.

Internal cleanup (no behavior change): renamed `_build_self_deallocate_script`'s
`sentinel_on_outer` parameter to `sentinel_to_remove` (clearer that `None` means no
sentinel to delete, i.e. the bare path), and dropped the now-unused
`sentinel_on_outer` parameter from the `_idle_watcher_service_unit` override.
