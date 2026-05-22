# Changelog - mngr_vps_docker

A concise, human-friendly summary of changes for the `mngr_vps_docker` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Lifted shared parallel-SSH discovery into the `VpsDockerProvider` base class behind a new `_list_provider_vps_hostnames()` seam method (concrete in the base, overridden by concrete providers); widened `os_id` to `int | str` so providers like OVH can carry friendly image names through build-args.
- Changed: `rsync` added to `cloud_init.generate_cloud_init_user_data`'s package list for belt-and-suspenders symmetry on cloud-init backends.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).
