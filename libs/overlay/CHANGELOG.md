# Changelog - overlay

A concise, human-friendly summary of changes for the `overlay` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Initial extraction of the layered config-merge algebra out of mngr into a standalone, dependency-free library: the key-suffix operators (`__extend` / `__assign`), the `Static*` atomic-value markers, the typed-node merge algebra (`lift` / `lower` / `combine` / `finalize` / `apply_extend` / `extend_plain_value`, plus the public `merge`, which raises `NarrowingError`, and `merge_narrowing_allowed`), and recursive narrowing detection (`would_assignment_narrow` / `narrowing_paths`).

### Changed

- Changed: Narrowing detection now reports the specific narrowed leaf path rather than the containing field. A new `narrowing_paths` predicate (the path-collecting counterpart of `would_assignment_narrow`) drives this: a same-keys dict whose nested value narrows yields the deep leaf path (e.g. `commands.create.defaults.env`), while a dropped dict key or a list/set narrowing still reports at the field. The raise/no-raise decision is unchanged — only the path strings are more precise.
