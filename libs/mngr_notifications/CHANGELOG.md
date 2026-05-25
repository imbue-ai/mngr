# Changelog - mngr_notifications

A concise, human-friendly summary of changes to the `mngr_notifications` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Watcher now fires its "agent is waiting for input" notification for the `RUNNING → UNKNOWN → WAITING` transition (provider temporarily unreachable, then recovers in `WAITING`), in addition to the existing direct `RUNNING → WAITING` transition.
