# Changelog - mngr_vultr

A concise, human-friendly summary of changes for the `mngr_vultr` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Now only contributes tag-listing; shared parallel-SSH VPS discovery has been lifted into the `VpsDockerProvider` base class behind a new `_list_provider_vps_hostnames()` seam method.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).
