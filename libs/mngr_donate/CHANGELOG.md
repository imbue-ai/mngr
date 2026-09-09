# Changelog - mngr_donate

A concise, human-friendly summary of changes for the `mngr_donate` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- New `imbue-mngr-donate` plugin, extracted from `imbue-mngr-usage`: provides `mngr donate`, which spends spare Claude capacity on a donation skill (default: scientific `document-review`). Depends on `imbue-mngr-usage` for the spare-capacity snapshot.
