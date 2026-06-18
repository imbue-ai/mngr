# Changelog - mngr_pi_coding_usage

A concise, human-friendly summary of changes for the `mngr_pi_coding_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.1] - 2026-06-18

## [v0.1.0] - 2026-06-16

### Added

- Added: New package `imbue-mngr-pi-coding-usage` providing cost/usage tracking for pi agents in `mngr usage`. pi reports per-message cost client-side, so it's REPORTED (not estimated) and aggregated session-incrementally. The per-message writer lives in `mngr_pi_coding`'s lifecycle extension (pi loads a single explicit extension); this package owns the reader (an `aggregate_usage_source` hookimpl claiming the `pi-coding` source) and provisions a `pi_emit_usage` gate marker so the extension only emits usage events when this package is installed.
