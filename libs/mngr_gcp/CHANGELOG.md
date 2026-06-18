# Changelog - mngr_gcp

A concise, human-friendly summary of changes for the `mngr_gcp` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

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
