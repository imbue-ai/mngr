# Changelog - mngr_usage

A concise, human-friendly summary of changes for the `mngr_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr usage` per-session cost aggregation with separate `subscription_cost` / `api_cost` aggregates, `--since`, `--detail`, and CEL/format-template surfaces; cost tracking now works for direct `ANTHROPIC_API_KEY` users.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `mngr usage` command reporting Claude Code's rolling 5h / 7d / overage quota usage with `human`/`json`/`jsonl` formats and the standard agent-filter flags.
- Added: New `mngr usage wait --until <CEL>` command that blocks until a usage snapshot matches a predicate, with exit codes mirroring `mngr wait`.
