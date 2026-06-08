# Changelog - mngr_lima

A concise, human-friendly summary of changes for the `mngr_lima` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.4] - 2026-06-05

## [v0.1.3] - 2026-06-01

### Added

- Added: Opt-in btrfs host-data volume mode for the Lima provider. New `is_host_data_volume_exposed: bool = True` field on `LimaProviderConfig` (and the matching persisted field on `LimaHostConfig`) controls how `host_dir` is backed: `True` keeps today's 9p bind-mount layout; `False` attaches a Lima-managed btrfs `additionalDisk` (`mngr-<host_id_hex>-data`, 100GiB default, qcow2 sparse) and symlinks `host_dir` to Lima's auto-mount path, so `host_dir` is snapshottable as a single consistent btrfs filesystem. `get_volume_for_host()` returns `None` in btrfs mode; the value is locked on the per-host record at create time so `stop_host` / `start_host` replay the same layout. New `host_data_disk_size` config field (default `"100GiB"`) and `limactl_disk_create` / `limactl_disk_delete` helpers. `destroy_host` / `delete_host` remove the named Lima disk when in btrfs mode.

## [v0.1.2] - 2026-05-28

### Changed

- Changed: Dropped `ssh-keyscan` from the host-creation flow — each Lima VM now gets a pre-generated ed25519 host keypair injected via the Lima `provision[mode=system]` script, eliminating the TOFU and the `Broken pipe` race during VM bring-up. Per-host keys live under `<provider-dir>/keys/hosts/<host_id>/`; `merge_lima_yaml` extends (rather than replaces) `provision` and `mounts` so mngr's load-bearing entries are preserved.
- Changed: `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix), matching what `limactl start` expects.
- Changed: Serial-log tailer switched from `tail --follow=name --retry` (GNU-only) to `tail -F` for macOS BSD-tail compatibility.

### Fixed

- Fixed: Lima provider now actually disables guest → host port forwarding — emits two ignore rules (`guestIP: 0.0.0.0` with `guestIPMustBeZero: true` and `guestIP: 127.0.0.1`), and `merge_lima_yaml` locks `portForwards` against user `--file` overrides.
