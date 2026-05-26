# Changelog - concurrency_group

A concise, human-friendly summary of changes for the `concurrency_group` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Adopted the new per-project changelog layout — per-PR entries now live under `libs/concurrency_group/changelog/`, with consolidated history in this project's `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md`.

### Fixed

- Fixed: `UNABRIDGED_CHANGELOG.md` intro now references the correct entries directory (`<project_dir>/changelog/`).

## [v0.2.8] - 2026-05-13

### Changed

- Changed: Background processes started via `ConcurrencyGroup.run_process_in_background()` now default to `is_checked_by_group=True`, so non-zero exits surface as `ProcessError` at group teardown instead of being silently swallowed.
