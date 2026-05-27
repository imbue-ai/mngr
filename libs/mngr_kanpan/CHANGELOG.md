# Changelog - mngr_kanpan

A concise, human-friendly summary of changes for the `mngr_kanpan` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: GitHub data source now refreshes via a single `gh api graphql` request per board cycle (filtered by `repo:` / `head:` qualifiers with `mergeable`, `statusCheckRollup`, `reviewThreads`, and `comments` embedded inline), replacing the four separate `gh` calls. Eliminates the gh HTTP cache race, drops the `_GH_PR_LIST_LOCK`, `ThreadPoolExecutor`, and conflicts/unresolved second-pass fetcher, and removes ~250 lines of fetch/parse/orchestration code.

## [v0.2.7] - 2026-05-11

### Added

- Added: `mngr_kanpan` field-value staleness — each `FieldValue` carries a `created` timestamp, taint propagates through cached inputs, and stale cells render dimmed; new `staleness_threshold_seconds` config.

### Fixed

- Fixed: `mngr kanpan` no longer logs per-agent CEL warnings for `--include` / `--exclude` filters that reference keys on tolerant schemaless fields.
