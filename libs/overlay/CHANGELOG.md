# Changelog - overlay

A concise, human-friendly summary of changes for the `overlay` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Initial extraction of the layered config-merge algebra out of mngr into a standalone, dependency-free library: the key-suffix operators (`__extend` / `__assign`), the `Static*` atomic-value markers, the typed-node merge algebra (`lift` / `lower` / `combine` / `finalize` / `apply_extend` / `extend_plain_value`, plus the public `merge`, which raises `NarrowingError`, and `merge_narrowing_allowed`), and recursive narrowing detection (`would_assignment_narrow` / `narrowing_paths`).
