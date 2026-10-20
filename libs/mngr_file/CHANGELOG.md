# Changelog - mngr_file

A concise, human-friendly summary of changes to the `mngr_file` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.14] - 2026-06-18

## [v0.1.13] - 2026-06-16

## [v0.1.12] - 2026-06-16

## [v0.1.11] - 2026-06-15

## [v0.1.10] - 2026-06-13

### Added

- Added: `mngr file list` now reports the full file type (file, directory, symlink, pipe, socket, block, character, other) and an opt-in `permissions` mode string when the source can report them — a host (online, or the local machine) classifies the real `stat`/`lstat` mode and shows a permissions string, while a bare volume-backed stopped host only distinguishes file vs. directory and leaves `permissions` as `-`. The default listing (name, type, size, modified) is unchanged.

### Changed

- Changed: `mngr file get`, `list`, and `put` now operate through the unified host file interfaces (online host or volume-backed stopped host, addressed by absolute paths under the host's `host_dir`) instead of branching internally between an online host and a separately-fetched volume; the per-command "volume path" computation and the duplicate cross-platform listing script are gone. Writing to a stopped host (offline `put`) still works through the volume-backed host's write interface; `--mode` continues to be ignored when the host is offline, and the existing "provider does not support volume access" error is unchanged.

### Fixed

- Fixed: `mngr file` See-Also references now link to `mngr rsync` instead of the removed `push` / `pull` commands, so the generated docs no longer contain broken `[mngr help push](mngr help push)` / `[mngr help pull](mngr help pull)` markdown links.

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28
