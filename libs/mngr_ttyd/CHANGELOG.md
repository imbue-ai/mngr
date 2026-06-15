# Changelog - mngr_ttyd

A concise, human-friendly summary of changes to the `mngr_ttyd` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.10] - 2026-06-13

## [v0.1.9] - 2026-06-08

## [v0.1.8] - 2026-06-05

## [v0.1.7] - 2026-06-01

## [v0.1.6] - 2026-05-28

### Fixed

- Fixed: `resources/ttyd_agent.sh` now uses `=$_SESSION:0` (the `=` exact-match prefix) when attaching to a named agent via URL arg, so the browser ttyd window no longer silently lands on a sibling-prefix session.
