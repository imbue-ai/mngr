# Changelog - overlay

A concise, human-friendly summary of changes for the `overlay` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Initial extraction of the layered config-merge algebra out of mngr into a standalone, dependency-free library: the key-suffix operators (`__extend` / `__assign`), the `Static*` markers, `apply_extend` / `extend_dict` / `combine_patches`, and the unified `merge` / `finalize` operations with recursive narrowing detection.
