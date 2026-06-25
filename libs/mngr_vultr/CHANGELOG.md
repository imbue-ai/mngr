# Changelog - mngr_vultr

A concise, human-friendly summary of changes for the `mngr_vultr` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Unauthenticated Vultr provider now raises the shared `ProviderNotAuthorizedError` at construction, instead of printing an ad-hoc `WARNING: Vultr API key not configured` and returning an empty listing (exit 0). The bespoke warning print has been removed. Reported consistently with the other cloud providers in `mngr list` rather than vanishing.

## [v0.1.10] - 2026-06-18

## [v0.1.9] - 2026-06-16

### Changed

- Changed: Vultr release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing modal/gcp/aws/ovh disables, so `mngr list` inside the Vultr lifecycle tests no longer exits non-zero when Azure credentials aren't resolvable.

### Removed

- Removed: Dead `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` stubs from `VultrVpsClient`, matching the removal of those abstract methods from the shared `VpsClientInterface`.

## [v0.1.8] - 2026-06-16

## [v0.1.7] - 2026-06-15

## [v0.1.6] - 2026-06-13

### Changed

- Changed: **Breaking** -- per-host build args renamed: `--vps-region=` is now `--vultr-region=` and `--vps-plan=` is now `--vultr-plan=`. The `--vps-os=` build arg is removed (`VultrVpsClient` now carries `os_id` locally; per-host overrides require a separate Vultr provider instance with its own `default_os_id`). The old `--vps-*` prefix raises a migration error. `--git-depth=` stays shared.
- Changed: Replaced a direct ValueError raise in Vultr provider config with a dedicated custom exception type.

## [v0.1.5] - 2026-06-08

## [v0.1.4] - 2026-06-05

## [v0.1.3] - 2026-06-01

### Changed

- Changed: **Breaking** — Vultr hosts created by `mngr create --provider vultr` now back their per-host unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`), enabling consistent `btrfs subvolume snapshot -r` snapshots. See `mngr_vps_docker`'s changelog for the full mechanism. Existing Vultr hosts created before this release cannot be discovered or managed after upgrade — destroy and recreate them.

## [v0.1.2] - 2026-05-28

### Changed

- Changed: `mngr_vultr` now only contributes the tag-listing; shared parallel-SSH discovery has been lifted into `VpsDockerProvider`.
