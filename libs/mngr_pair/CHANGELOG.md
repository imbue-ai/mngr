# Changelog - mngr_pair

A concise, human-friendly summary of changes to the `mngr_pair` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr pair` works with agents on remote hosts, running a second unison on the host over mngr's SSH transport. Both ends need unison 2.52+ and `unison-fsmonitor`; when the host lacks a usable one, mngr installs a pinned, checksum-verified static unison 2.54.0 into `~/.mngr/bin` (Linux x86_64 only).
- Added: Pairing with a host rather than an agent: `--source-host` with an absolute `--source-path` syncs that directory without an agent (git sync is refused in that mode).
- Added: `--start/--no-start`, `--ignore-archives` (for a freshly created directory, instead of unison refusing to run) and `--links/--no-links` (default `--links`) flags.
- Added: `--format jsonl` emits `pair_syncing` once unison is watching both replicas and `pair_transferring` (`is_transferring`, `bytes_done`, `bytes_total`) as transfers start and settle; human output shows `Transferring... (12.0 MB of 17.0 MB)`.

### Changed

- Changed: The initial git reconciliation fetches the agent's branch into your local repository instead of fetching your repository into the agent's.
- Changed: `.git` is excluded from the sync only in git-reconciling mode (the default); with `--no-require-git` it is no longer dropped automatically (pass `--exclude .git` to keep excluding it).

### Fixed

- Fixed: Stopping `mngr pair` run as a supervised subprocess (SIGINT or `SIGTERM`) left `unison` running and still syncing; both now stop it.
- Fixed: `--include` never propagated anything in continuous mode; such a sync now polls every couple of seconds instead of watching.
- Fixed: Unbounded memory growth over a long pairing session from retained unison output.

## [v0.2.17] - 2026-06-18

## [v0.2.16] - 2026-06-16

## [v0.2.15] - 2026-06-16

## [v0.2.14] - 2026-06-15

## [v0.2.13] - 2026-06-13

## [v0.2.12] - 2026-06-08

## [v0.2.11] - 2026-06-05

## [v0.2.10] - 2026-06-01

## [v0.2.9] - 2026-05-28
