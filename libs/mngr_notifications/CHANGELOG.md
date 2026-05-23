# Changelog - mngr_notifications

A concise, human-friendly summary of changes to the `mngr_notifications` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Watcher now recognizes the indirect `RUNNING → UNKNOWN → WAITING` transition and fires its "agent is waiting for input" notification for it, in addition to the existing direct `RUNNING → WAITING`. Carries a per-agent "was RUNNING before going UNKNOWN" bit, cleared on any other transition out of UNKNOWN.

### Changed

- Changed: Project adopted the per-project changelog layout (`changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).
