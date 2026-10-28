# Changelog - mngr_docker

A concise, human-friendly summary of changes for the `mngr_docker` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.1] - 2026-10-28

## [v0.1.0] - 2026-10-22

### Added

- Added: New `imbue-mngr-docker` plugin holding the docker provider, split out of `imbue-mngr`. `mngr create @.docker`, `mngr docker resize`, `mngr help docker_usage` and `[providers.docker]` settings are unchanged but need the plugin (`uv tool install imbue-mngr --with imbue-mngr-docker`, or `mngr plugin add imbue-mngr-docker`). The docker error classes moved to `imbue.mngr_docker.errors` (names unchanged); the backend registers lazily, so `mngr --help`, `mngr config` and `mngr list` no longer import the docker SDK.

- Added: Create steps reported on the structured progress stream (`--format jsonl`): `docker.build` (advancing per Dockerfile stage) or `docker.pull`, `docker.start_container`, `docker.wait_ssh`.

- Added: Docker discovery reports each host's recorded size (`DiscoveredHost.size`), carried in the `mngr observe` stream.

- Added: `DOCKER_HOST_ACCESS_MANIFEST` (`constants.py`) publishes the per-host key layout (`keys/host_keys/<host uuid hex>/`, pins in `keys/known_hosts`).

### Changed

- Changed: Configuring a host's sshd logs, at info, the fingerprints of the keys replacing its `authorized_keys`.
