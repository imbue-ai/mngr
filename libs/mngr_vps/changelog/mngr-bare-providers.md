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
