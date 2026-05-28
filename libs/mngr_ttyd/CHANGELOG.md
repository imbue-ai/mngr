# Changelog - mngr_ttyd

A concise, human-friendly summary of changes to the `mngr_ttyd` project. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Fixed

- Fixed: `resources/ttyd_agent.sh` now uses exact-session matching (`=$_SESSION:0`) when attaching to a named agent via URL arg, so the browser ttyd window can no longer be silently routed to a sibling session whose name starts with the requested one.
