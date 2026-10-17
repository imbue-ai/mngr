# Changelog - mngr_opencode_usage

A concise, human-friendly summary of changes for the `mngr_opencode_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.1] - 2026-06-18

## [v0.1.0] - 2026-06-16

### Added

- Added: New package `imbue-mngr-opencode-usage` providing cost/usage tracking for opencode agents in `mngr usage`. Installs a second in-process opencode plugin (alongside the lifecycle one) that appends one `cost_snapshot` event per assistant message (opencode's reported per-message cost, plus tokens and the provider-qualified model) to `events/opencode/usage/events.jsonl`. A reader hookimpl claims the `opencode` source and aggregates session-incrementally. After provisioning, an opencode agent's spend shows up in `mngr usage` like Claude's.
