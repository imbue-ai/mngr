# Changelog - mngr_recursive

A concise, human-friendly summary of changes to the `mngr_recursive` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.14] - 2026-06-18

## [v0.1.13] - 2026-06-16

## [v0.1.12] - 2026-06-16

## [v0.1.11] - 2026-06-15

## [v0.1.10] - 2026-06-13

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

### Fixed

- Fixed: `mngr create` on remote hosts (e.g. Modal) no longer fails during provisioning with `Error reading SSH protocol banner` / `Connection reset by peer` (github issue 1825). Deploy files are now uploaded with a single rsync transfer instead of one SFTP channel per file; the old per-file approach opened a fresh SFTP channel per file (~0.7s/file over Modal), so a user's `~/.claude/plugins` tree (hundreds of files) could exceed the upload timeout or reset the connection mid-transfer.

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28
