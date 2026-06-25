# Changelog - mngr_gcp

A concise, human-friendly summary of changes for the `mngr_gcp` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Offline `host_dir` support for the GCP provider (matching AWS/Azure). A stopped GCE instance's `host_dir` is now readable without starting it (so `mngr event` / `mngr transcript` / `mngr file` work against it), captured operator-side at `mngr stop` and uploaded to a Google Cloud Storage state bucket. `mngr gcp prepare` now also creates a GCS state bucket (named `mngr-state-<project_id>` by default, configurable via `[providers.gcp] state_bucket_name`); `mngr gcp cleanup` deletes that bucket alongside the firewall rule, with a new `--force` flag that opts into deleting it even when it still holds offline host state. New config fields: `state_bucket_name` and `is_offline_host_dir_enabled` (default on).

- Added: Bare placement (`isolation=NONE`) — the agent runs directly on the VM (no Docker), reached at `vps_ip:22` as root. The idle agent runs `shutdown -P now`, which on GCE stops the instance, so the container-only sentinel watcher is skipped. A running bare host is now discoverable with the default provider config (no need to re-specify `-S providers.<name>.isolation=NONE` at connect time) via a `mngr-isolation` instance-metadata marker stamped at create.

### Changed

- Changed: SSH host keys are now unique per host (inherited from the shared VPS provider): each host gets its own VPS/VM-root and container sshd host keypair at create time rather than sharing one keypair across every host the provider instance created. Pause/resume of hosts created before this change still works via a fallback to the legacy provider-global key.

- Changed: A missing/unresolvable ADC credential or project now raises the shared `ProviderNotAuthorizedError` (still a `ProviderUnavailableError`), so `mngr list` surfaces one consistent error line and a non-zero exit, matching the other cloud providers.

- Changed: GCP's offline host/agent store now holds the *full* host record (config, IP, host keys) instead of a lossy field subset, matching AWS/Azure. A stopped GCE instance's `mngr list` / `mngr start` reconstructs the complete record from a single `mngr-host-state` instance-metadata value (plus one `mngr-agent-<id>` value per agent), rather than the previous label-only reconstruction.

- Changed: mngr host identity (host id and created-at) moved out of GCE labels and into instance metadata, joining host name and per-agent records. Only `mngr-provider` remains a label (the discovery filter). Host id is now stored verbatim and created-at as ISO-8601 (no more GCE-charset lowercasing). Backward-incompatibility: a GCE instance created before this change carries its host id / created-at only in labels, so an *already-running* pre-upgrade host will no longer resolve by id for offline discovery / `mngr start`; destroy and recreate such hosts (online hosts reachable over SSH are unaffected).

- Changed: GCP hosts inherit the shared VPS host-setup fix that registers the gVisor (runsc) runtime with `--overlay2=none`, so an agent container's writable layer persists across a `docker stop`/`start` or host reboot instead of being lost.

- Changed: The `project_id` config field now defaults to `None` instead of `""`, making "unset" explicit and matching the other optional identifier fields. Resolution behavior is unchanged: an unset `project_id` still falls back to the project ADC resolves from the environment.

### Fixed

- Fixed: Renaming a host now re-stamps the `mngr-host-name` instance metadata that offline discovery reads, so a host renamed and then stopped lists under its new name (previously it stayed listed under its old name).

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
