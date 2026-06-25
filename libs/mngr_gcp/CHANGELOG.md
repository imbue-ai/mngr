# Changelog - mngr_gcp

A concise, human-friendly summary of changes for the `mngr_gcp` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Bare placement support (`[providers.gcp].isolation = "NONE"`) — the idle agent runs `shutdown -P now` as the VM's root, which on GCE stops the instance. Added bare-placement release tests.
- Added: GCP hosts inherit the shared VPS host-setup fix that registers the gVisor (runsc) runtime with `--overlay2=none`, so an agent container's writable layer persists across a `docker stop`/`start` or host reboot instead of being lost to the default per-sandbox overlay.

### Changed

- Changed: Moved mngr host identity (host id and created-at) out of GCE *labels* and into instance *metadata*, joining the host name and per-agent records already kept there. Only `mngr-provider` remains a label (the server-side `instances.list` discovery filter). Host id is now stored verbatim and created-at as an ISO-8601 timestamp (no more GCE-charset lowercasing / `%Y-%m-%dt%H-%M-%S` encoding). **Backward-incompatibility:** a GCE instance created before this change carries its host id / created-at only in labels, so an *already-running* pre-upgrade host will no longer resolve by id for offline discovery / `mngr start` and its reconstructed created-at falls back to now() — destroy and recreate such hosts (online hosts reachable over SSH are unaffected).
- Changed: `stop_host` / `start_host` moved to the shared `OfflineCapableVpsProvider`; GCP now supplies only the GCE `_pause_cloud_instance` / `_resume_cloud_instance` hooks. The idle-watcher install and the best-effort `_on_host_finalized` step runner moved to the shared base; the host-side idle-watcher systemd unit name changed from `mngr-gcp-idle-watcher` to the shared `mngr-idle-watcher`. Behavior-preserving.
- Changed: `mngr gcp prepare` / `cleanup` now resolve their `[providers.<name>]` block and refuse-on-existing-instances via the shared `mngr_vps.cli_helpers`. `GcpProviderConfig` lifts `allowed_ssh_cidrs` into a shared config base. The cleanup refusal when instances still exist now raises the unified `ManagedResourcesExistError` (previously `GcpError`) so the message matches the other clouds. `allowed_ssh_cidrs` is now typed `ScalarStrTuple`, so a higher-precedence config layer that sets it replaces the whole list rather than being flagged as narrowing.
- Changed: The GCP provider's `project_id` config field now defaults to `None` instead of `""`, making "unset" explicit and matching the other optional identifier fields. Resolution behavior is unchanged.

### Fixed

- Fixed: A running bare (`isolation=NONE`) host is now discoverable and reachable with the default provider config — `mngr conn`/`list`/`stop`/`start`/`destroy` no longer need `-S providers.<name>.isolation=NONE` at connect time. GCE instances now carry a `mngr-isolation` value in instance metadata (where GCP keeps mngr identity; GCE labels are too restricted), stamped at create.
- Fixed: Renaming a host now re-stamps the `mngr-host-name` instance metadata (the cheap identity tag offline discovery reads), not just the host record. Previously this metadata was written only at create, so a renamed-then-stopped host still listed under its old name in offline discovery.
- Fixed: `start_host` for a bare host. It read the host record via the Docker volume, which a bare host does not have, so it now resolves the store through the realizer.

## [v0.1.2] - 2026-06-18

### Added

- Added: Native GCE stop/start lifecycle (idle-pause + resume) for GCP hosts. `mngr stop` stops the GCE instance (preserving the boot disk so a paused agent costs only disk storage), `mngr start` resumes it (rebinding known_hosts to the fresh external IP). Stopped instances stay discoverable via instance metadata + labels, so `mngr list` and `mngr start <agent>` keep working while TERMINATED. An in-container idle watcher self-stops the instance via a host-side systemd path/service unit.

### Changed

- Changed: GCP's stopped-host offline discovery/resolution, stop/start lifecycle, known_hosts rebinding, and idle-watcher install now come from the shared `OfflineCapableVpsDockerProvider` base; GCP supplies only the GCE-specific hooks. No behavior change.

## [v0.1.1] - 2026-06-16

### Changed

- Changed: `mngr gcp prepare` / `mngr gcp cleanup` group their GCP-specific options under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options instead of below them.
- Changed: GCP release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/aws/vultr/ovh disables, so `mngr list` inside the GCP lifecycle tests no longer exits non-zero when Azure credentials aren't resolvable.

### Removed

- Removed: Dead `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` stubs (and the now-unused `_boot_disk_source` helper, snapshots compute client, and `FakeSnapshotsClient` test helper) from `GcpVpsClient`, matching the removal of those abstract methods from the shared `VpsClientInterface`.

## [v0.1.0] - 2026-06-16
