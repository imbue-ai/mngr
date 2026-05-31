# Changelog - mngr_vultr

A concise, human-friendly summary of changes for the `mngr_vultr` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Vultr hosts created by `mngr create --provider vultr` now back their per-host unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem on the VPS (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`), making future consistent `btrfs subvolume snapshot -r` snapshots of the agent data possible. See `mngr_vps_docker`'s changelog for the full mechanism. **Breaking change:** existing Vultr hosts created before this release cannot be discovered or managed after upgrade; destroy and recreate them.

## [v0.1.2] - 2026-05-28

### Changed

- Changed: `mngr_vultr` now only contributes the tag-listing; shared parallel-SSH discovery has been lifted into `VpsDockerProvider`.
