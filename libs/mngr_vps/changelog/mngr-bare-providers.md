Introduced a `HostRealizer` seam inside the VPS provider as the first step toward
running agents directly on a cloud VM (no Docker container). The provider now
selects a realizer from a new `isolation` config knob (`IsolationMode.CONTAINER`
| `NONE`); `CONTAINER` is the default and preserves the original behavior
exactly. All Docker-container placement logic (image build/pull, container run,
in-container sshd setup, btrfs volume + snapshot helper, container
stop/start/teardown, and `docker commit` snapshots) moved behind a
`DockerRealizer` that the provider's base methods delegate to. The agent SSH
endpoint, placement lifecycle, and snapshots are now realizer concerns, while the
machine (provisioning, boot, instance lifecycle, host record, discovery) stays
with the provider.

Host-record store resolution also moved behind the realizer
(`realizer.open_host_store(outer, host_id)`), so a non-Docker placement can
persist its host record without a Docker volume. The container realizer
resolves the per-host Docker volume exactly as before.

Added the `BareRealizer`: it places the agent directly on the VM's OS (no
Docker), reached at `vps_ip:22` as root with the same VPS keypair the provider
already uses for the outer. It installs the lightweight host packages and mngr
host_dir layout on the VM (the same setup the container gets, applied to the OS),
keeps the host record in a plain root-disk directory, and reports no snapshot
support. Machine stop/start/destroy stays the substrate's job, so the bare
placement lifecycle steps are no-ops.

Discovery and listing also moved behind the realizer: finding the host on a
VPS, reading its running state, and collecting the live agent listing are now
realizer methods (`find_host_record`, `read_live_listing`, `is_placement_running`,
`collect_listing_output`). The container realizer keeps the exact Docker probes
(`docker ps` label lookup, `docker inspect`, `docker exec`); the bare realizer
reads the record from the fixed store path and runs the listing script directly
on the VM. Behavior-preserving for Docker.

The `AGENT_TAG_FIELDS` constant (used by the AWS/Azure tag-mirror code) is now
public, matching its sibling `AGENT_TAG_PREFIX`, so it is no longer imported as
a private name across modules.

`VpsHostConfig.container_name`/`volume_name` are now nullable so a bare host
record (which has no container or Docker volume) is representable, and the
agent-sshd wait now targets the realizer's endpoint port (the container port
for Docker, port 22 for bare) instead of hard-coding the container port.

`isolation=NONE` now builds the `BareRealizer`. The idle-shutdown action is a
realizer concern: the container realizer signals the container's PID 1
(`kill -TERM 1`), while the bare realizer powers the VM off directly
(`shutdown -P now`) -- so on a self-stopping cloud substrate a bare placement
needs no host-side sentinel/systemd watcher. The host_dir's outer-filesystem
path is also realizer-driven now (the btrfs subvolume for container; the fixed
root-disk store for bare), so the offline host_dir sync targets the right path
in both shapes.

Bare placement is gated to providers with a machine stop/start lifecycle: a
provider without one rejects `isolation=NONE` at create time
(`BareIsolationNotSupportedError`) rather than strand a VM it cannot restart.
AWS, GCP, and Azure all enable it. AWS/GCP bare self-stops the instance via the
OS-shutdown behavior; Azure bare instead runs the ARM self-deallocate from its
idle `shutdown.sh` (an Azure OS shutdown does not halt compute billing), reusing
the same deallocate call the container watcher uses and keeping the
self-deallocate role assignment. No user-visible behavior change for existing
container hosts on any provider.

Renamed the package from `mngr_vps_docker` to `mngr_vps` (the distribution
`imbue-mngr-vps-docker` to `imbue-mngr-vps`), since Docker is now one of two
placement shapes rather than the whole package. The shape-agnostic classes
dropped "Docker" from their names: `VpsDockerProvider` -> `VpsProvider`,
`VpsDockerProviderConfig` -> `VpsProviderConfig`, `MinimalVpsDockerProvider` ->
`MinimalVpsProvider`, `OfflineCapableVpsDockerProvider` ->
`OfflineCapableVpsProvider`, `TagMirrorVpsDockerProvider` ->
`TagMirrorVpsProvider`, `VpsDockerHostRecord` -> `VpsHostRecord`,
`VpsDockerHostStore` -> `VpsHostStore`, and the error base `VpsDockerError` ->
`VpsError`. The genuinely Docker-specific `DockerRealizer` and the
`container_setup` helpers keep their names. Mechanical rename; no behavior
change.

A bare create also rejects container-only inputs up front (an image override, a
Dockerfile build, or docker run start-args) rather than silently ignoring them.

Bugfix (found by the new bare release tests): on resume, the aws/gcp/azure
`start_host` read the host record through the Docker volume (`docker volume
inspect`), which does not exist for a bare host, so `mngr start` failed. It now
resolves the store through the realizer (the fixed root-disk path for bare).

The VPS provider's host-record updates use the type-safe `model_copy_update` /
`to_update` idiom instead of `model_copy(update={...})`, so field names are
checked by the type system.

The shared ``OfflineCapableVpsProvider`` now owns the cloud stop/start lifecycle: ``stop_host`` pauses the whole instance (so a paused agent costs only disk) and ``start_host`` resumes it, doing the resumed record's on-volume write *and* its external-store mirror together in one place. Providers supply only the cloud-API hooks (``_pause_cloud_instance`` / ``_resume_cloud_instance``) and override ``_sync_host_dir_before_pause`` / the known_hosts rebind where their behavior differs.

Scrubbed leftover "VPS Docker" wording from the provider's user-facing strings
(the config field description, the host-created success log, the
discover_hosts_and_agents log span, and the mutable-tags error messages), since
the provider now supports both container and bare placements. Removed the
redundant `_host_dir_path_on_outer` forwarder in favor of calling the realizer's
`host_dir_path_on_outer` directly.

Lifted three structurally-duplicated subsystems out of the aws/gcp/azure backends into the shared `OfflineCapableVpsProvider`, with small per-provider hooks:

- The self-stopping idle watcher (in-container sentinel `shutdown.sh`, the host-side systemd `.path`/`.service` install, and the bare-placement shutdown script) is now shared. The systemd units use a single shared name (`mngr-idle-watcher`); providers customize the `.service` body via `_idle_watcher_service_unit` (AWS/GCP default to `shutdown -P now`; Azure overrides to its ARM self-deallocate) and prepare the outer via `_prepare_idle_watcher_outer` (Azure installs curl + its deallocate script). The bare shutdown action is `_write_bare_idle_shutdown_script` (default: the realizer's poweroff; Azure: ARM deallocate, since an Azure OS shutdown does not halt billing).

- The host_dir-to-bucket sync daemon (install of the oneshot `.service` + `.timer`, and the before-pause flush) is now shared under the `mngr-host-dir-sync` unit name, gated on `_is_host_dir_sync_enabled` (off by default, so GCP installs nothing). Providers supply the sync CLI install (`_host_dir_sync_install_command`), the per-host `.service` body (`_host_dir_sync_service_unit`), and the target URI (`_host_dir_sync_target_uri`).

- `_on_host_finalized` is now a shared best-effort step runner: each step's failure is logged at WARNING and the rest still run, preserving the prior non-fatal contract. Providers extend the step list via `_post_finalize_steps` (Azure prepends its self-deallocate role assignment).

No user-visible behavior change; the host-side systemd unit names changed from per-provider (`mngr-aws-idle-watcher` etc.) to the shared `mngr-idle-watcher` / `mngr-host-dir-sync`.

Snapshot support is now a structural fact rather than a method that raises.
`snapshot_placement` / `delete_snapshot_placement` moved off the base
`HostRealizer` into a narrow `SnapshotCapableRealizer` sub-interface that only
the container realizer implements; the bare realizer is a plain `HostRealizer`
with no snapshot bodies. The provider gates its public snapshot operations once
at its boundary: a snapshot request on a bare placement now fails up front with
`SnapshotsNotSupportedError` instead of reaching into the realizer mid-operation.
No behavior change for container hosts; a bare snapshot still fails with a clear
error (just earlier).

The placement "is running" predicate is now computed exactly one way. Previously
the container realizer derived running-state two ways -- a cheap `docker inspect`
probe (`is_placement_running`, used by `get_host` / `discover_hosts` without a
full listing read) versus parsing the listing script's `CONTAINER_STATE` -- which
could disagree. Both now route through a single `is_running_container_state`
rule, and the cheap probe reads `.State.Status` (the same string the listing
emits) instead of `.State.Running`. The cheap probe path is preserved: the
get-host / discover callers still learn is-running without triggering the heavy
listing script. No behavior change.

The realizer's placement-lifecycle methods now take an opaque `PlacementHandle`
(the container name/id/volume bundle for the container realizer; empty for bare)
instead of the whole `VpsHostRecord`. The realizer mints the handle in
`realize_placement` and the provider extracts it once from the persisted record
at each call boundary (`PlacementHandle.from_record`), so the repeated
`record.config.container_name` reads (and their `assert record.config is not None
and record.config.container_name is not None` guards) collapse to a single
boundary extraction, the bare realizer's "ignore the record" becomes an explicit
empty handle, and `start_activity_watcher` no longer takes a `container_name`
parameter (the realizer reads it from its handle). Internal refactor; no
user-visible behavior change.

Moved the pure VPS build-arg parsing helpers (`ParsedVpsBuildOptions`, `extract_single_value_arg`, `extract_git_depth`, `extract_presence_flag`, `parse_vps_build_args`, `raise_if_vps_migration_arg`, `raise_if_unknown_provider_arg`) out of `instance.py` into a new `imbue.mngr_vps.build_args` module. Mechanical extraction; no behavior change.

Moved the offline / tag-mirror provider subsystem (`OfflineCapableVpsProvider`, `TagMirrorVpsProvider`, and their idle-watcher / host_dir-sync unit builders and agent-tag constants) out of `instance.py` into a new `imbue.mngr_vps.instance_offline` module. `instance_offline` imports `VpsProvider` from `instance`; the dependency is one-directional. Mechanical extraction; no behavior change.

Extracted the offline read-side reconstruction shared by the tag-mirror (AWS/Azure) and GCE-metadata (GCP) providers into a new `KeyValueMirrorVpsProvider` base in `instance_offline`, sitting between `OfflineCapableVpsProvider` and `TagMirrorVpsProvider` (which now extends it). The base owns reconstruction over a `dict[str, str]` key-value mirror: reassembling per-agent records, computing the per-field upsert/delete set (`_agent_field_items`), building STOPPED discovered/offline hosts, parsing the created-at value, and resolving instances by host id. Providers supply the map (`_offline_kv_map`), the optional per-value length cap (`_max_value_len`, 256 for tags and uncapped for metadata), and the host-name key (`_host_name_key`, renamed from the old `_host_name_tag_key`). The bucket-or-tags routing and the external `_state_store` stay on `TagMirrorVpsProvider`; GCP inherits no bucket machinery, and the per-provider agent-record write side is unchanged. Internal refactor; no user-visible behavior change.

Hardened systemd unit generation. A new `imbue.mngr_vps.systemd.render_systemd_unit` helper centralizes the unit-file format so the offline-capable provider's `.path` / `.service` / `.timer` units are no longer hand-assembled as `[Section]\nKey=Value\n` strings. The idle-watcher poweroff action moved from an inline `ExecStart=/bin/sh -c 'rm -f <sentinel> && shutdown -P now'` to an installed `/usr/local/sbin/mngr-idle-watcher.sh` script (written by the default `_prepare_idle_watcher_outer`), matching the existing Azure self-deallocate script pattern, so the sentinel path no longer has to survive systemd's plus the shell's nested quoting. The host_dir-sync command is likewise installed at `/usr/local/sbin/mngr-host-dir-sync.sh` (see `build_host_dir_sync_script`) and referenced by `ExecStart` directly. No behavior change.

Deduplicated the cloud state-bucket / offline-read-volume layer. A new
`BaseObjectStoreVolume` in `state_bucket_base.py` implements the six `Volume`
methods (`listdir`, `path_exists`, `read_file`, `remove_file`,
`remove_directory`, `write_files`) plus the `_as_dir_prefix` normalization once,
in terms of a small set of per-cloud SDK primitives (`_iter_delimited_entries`,
`_prefix_has_any_object`, `_has_object_at_key`, `_read_object_bytes`,
`_delete_single_object`, `_delete_prefix`, `_write_object`) and a normalized
`ObjectStoreEntry` listing shape; `S3Volume` / `BlobVolume` become thin
subclasses. A shared `_ObjectStoreErrorSeam` (`_translate_errors` /
`_is_not_found` / `_bucket_error_type`) lets the base run each SDK op inside the
cloud's error translation and special-case not-found, and `_get_object` /
`_delete_object` / `_prefix_has_objects` now live on `BaseStateBucket` in terms
of that seam. Internal refactor; no user-visible behavior change (the only edge
the two clouds previously disagreed on -- `path_exists("")` on an unscoped raw
volume, never reached in practice -- is now uniformly `False`).

Added a shared `cli_helpers` module for the cloud providers' operator CLIs: `resolve_provider_config` (the `[providers.<name>]` lookup + wrong-backend warning + class-defaults fallback) and `refuse_if_managed_resources_exist` (the cleanup-refusal guard that blocks deleting shared network infrastructure while mngr-managed instances still exist). The AWS / Azure / GCP / OVH CLIs now delegate to these instead of carrying near-identical copies. The refusal now raises a single `ManagedResourcesExistError` (a `VpsError` / `MngrError`) across all providers, so `mngr <cloud> cleanup` renders the "refusing to clean up" message identically whichever cloud you are on. Added config bases `OfflineCapableVpsProviderConfig` (carries `allowed_ssh_cidrs`, shared by AWS/Azure/GCP) and `PublicIpVpsProviderConfig` (adds `associate_public_ip`, shared by AWS/Azure only -- GCP names its equivalent field `associate_external_ip`, so it extends the former directly), with `allowed_ssh_cidrs` unified to `ScalarStrTuple`. No user-facing config key changed.

Lifted more cross-provider glue out of the aws/azure/gcp backends into the shared offline layer (`instance_offline.py`), with small per-provider hooks. The near-identical EC2/VM tag-mirror `HostStateStore` is now a single `TagHostStateStore` typed over `TagMirrorVpsProvider`, with the only cloud-specific step (the tag-removal API call) behind a new `_remove_instance_tags` hook (and `_persist_agent_to_tags` now declared abstract on the base). The object-storage providers' shared host_dir-sync install (the systemd `.service`/`.timer` write sequence) moved up into `BucketHostDirBackend.install_sync`; each provider supplies a `HostDirSyncInstallPlan` (install command, sync command, `.service` body, target URI) or `None` to skip, plus a `_cloud_label` hook. The `_state_store` selection (bucket when present, else the tag store) is now concrete on `TagMirrorVpsProvider`, driven by new `_state_bucket` / `_bucket_error_type` / `_bucket_label` hooks; the cloud-specific bucket-existence probe stays per-provider. `_list_provider_vps_hostnames` (cached listing -> non-empty `main_ip`) is now the default on `KeyValueMirrorVpsProvider`. A new `VpsProvider._require_parsed` helper replaces each provider's hand-written `match`/type-narrowing guard in `_create_vps_instance`. No user-visible behavior change.

Localized internal cleanups (no user-visible behavior change): a shared `_run_provisioning_step` helper collapses the repeated "run an outer command, raise `VpsProvisioningError` with its stderr on failure" shape across the btrfs/dir/systemd provisioning steps in `container_setup`; the four structurally-identical `teardown_placement` cleanup blocks in `docker_realizer` route through a single `_record_cleanup_attempt` helper; a `VpsHostRecord.with_certified_updates` method wraps the "update the nested certified data, then re-wrap the record" idiom at its three call sites; a `VpsProvider._write_and_mirror` method pairs every on-volume `write_host_record` with its external-store mirror in one place (the two had a history of drifting apart); a `VpsProvider._write_shutdown_script` method shares the `commands/shutdown.sh` mkdir/write/chmod plumbing across the container/bare/sentinel/deallocate variants; and the duplicated base64 remote-script wrapper (`_remote_sh_command`) is gone in favor of the shared `host_setup.build_remote_script_command` (renamed from the private `_remote_script_command`).

Bugfix (found by the GCP bare release test): on resume, the shared
`OfflineCapableVpsProvider.start_host` waited only for *any* sshd handshake
(`wait_for_sshd`) before its strict-host-key-checked connect, but never waited for
the VM to actually serve mngr's expected host key the way create does. On GCP the
GCE startup-script re-runs on every boot and `systemctl restart ssh`s partway
through, so the any-key wait could return inside that restart window and the
strict connect then hit a refused/mismatched port 22 -- failing `mngr start` with
"Unable to connect to port 22". This surfaced for bare placement specifically,
whose agent endpoint *is* port 22 (containers reach the agent on a separate,
stable container port). `start_host` now mirrors create's host-key wait
(`_wait_for_expected_host_key` on port 22, using mngr's VPS host public key) right
after the sshd wait, in the shared base. Cloud-init backends (AWS/Azure) inherit
the no-op default and return on the first poll; GCP's existing override polls
until its startup-script-installed key is live, riding out the ssh restart.

Integrated the `mngr/volumes` offline-store simplification (commit `f8bb5c0a5`): the per-agent instance-tag mirror is removed in favor of a single uniform external `HostStateStore` per provider -- AWS/Azure use their object-storage state bucket as the sole offline store (a stopped host's offline metadata now requires the bucket; the provider's `_state_store` raises an actionable `missing_state_bucket_error` pointing at `mngr <cloud> prepare` when the bucket is absent), and GCP uses a lossless instance-metadata-backed store (full host record + one JSON value per agent). AWS/Azure/GCP now extend `OfflineCapableVpsProvider` directly. This supersedes the earlier-on-this-branch tag-mirror dedup (the lifted `TagHostStateStore` / `KeyValueMirrorVpsProvider` / `TagMirrorVpsProvider` are gone); the realizer architecture, the systemd-unit hardening, and the cli/config/state-bucket dedup are retained. No behavior change for container hosts beyond the offline-metadata-requires-bucket consequence noted above.
