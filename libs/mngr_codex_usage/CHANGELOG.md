# Changelog - mngr_codex_usage

A concise, human-friendly summary of changes for the `mngr_codex_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.2] - 2026-06-18

## [v0.1.1] - 2026-06-16

### Added

- Added: New package `imbue-mngr-codex-usage` providing cost/usage tracking for Codex agents in `mngr usage`. Codex reports cumulative token usage (no dollar cost), so cost is estimated from the pricing table and aggregated session-cumulatively. The writer (`codex_usage.sh`) reads codex's rollout stream and emits one `cost_snapshot` per `token_count` item; `mngr_codex`'s background-tasks supervisor launches it when present. Codex's 5h/7d rate-limit windows (subscription mode) are mapped so Codex subscription agents get Claude-style windows. The writer tracks a byte-offset cursor (persisted under `plugin/codex/.usage_cursor`) so each poll processes only the new tail of the rollout.
