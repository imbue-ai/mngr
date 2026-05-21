# Changelog - mngr_vps_docker

A concise, human-friendly summary of changes for the `mngr_vps_docker` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `rsync` to the cloud-init package list in `generate_cloud_init_user_data` for symmetry with `mngr_ovh`'s `install_required_outer_packages` on non-cloud-init paths.

### Changed

- Changed: Lifted the shared parallel-SSH VPS discovery into the `VpsDockerProvider` base class behind a new `_list_provider_vps_hostnames()` seam method (concrete in the base, returns `[]`); `mngr_vultr` now only contributes tag-listing.
- Changed: Widened `os_id` in the VPS Docker base to `int | str` so providers (like OVH) can carry friendly image names through the existing build-args parser without disrupting integer-id providers (like Vultr).
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).
