# Changelog - mngr_notifications

A concise, human-friendly summary of changes to the `mngr_notifications` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: `mngr notify` detects a running observer with mngr's read-only `is_observe_writer_running` probe: it no longer creates a lock file in a host dir that never had an observer, sees an observer whose lock file another user owns, and starts an observer when the probe itself fails.

### Fixed

- Fixed: The watcher's RUNNING -> UNKNOWN -> WAITING notification tracking is keyed by agent instance (`<agent_id>@<host_id>`), so same-id agents on different hosts no longer share it.

## [v0.1.14] - 2026-06-18

## [v0.1.13] - 2026-06-16

## [v0.1.12] - 2026-06-16

## [v0.1.11] - 2026-06-15

## [v0.1.10] - 2026-06-13

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28

### Changed

- Changed: The watcher's "agent is waiting for input" notification now fires for the indirect `RUNNING → UNKNOWN → WAITING` transition (when the agent's provider was briefly unreachable) in addition to the direct `RUNNING → WAITING` transition; an internal per-agent bit bridges the indirect path.
