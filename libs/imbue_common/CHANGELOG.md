# Changelog - imbue_common

A concise, human-friendly summary of changes for the `imbue_common` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.20] - 2026-06-13

### Changed

- Changed: Constrained `primitives` types and the `RegexPattern` validator now raise dedicated `InvalidPrimitiveValueError` / `InvalidRegexPatternError` instead of `ValueError`.

### Fixed

- Fixed: `PREVENT_BUILTIN_EXCEPTION_RAISES` ratchet regex now catches `raise ValueError("literal")` and `raise OSError()` forms (a trailing `\b` after the opening paren had limited it to raises whose first arg started with a word character) and excludes test files.

## [v0.1.19] - 2026-06-05

### Added

- Added: `check_per_file_host_upload` ratchet (and `find_per_file_host_uploads_in_loops` AST helper) in the shared `ratchet_testing` framework. Flags `write_file` / `write_text_file` / `put_file` calls inside `for` / `while` loops, steering bulk transfers toward a single rsync (`host.copy_directory`).

### Fixed

- Fixed: Ratchet file scans no longer crash on a tracked symlink that resolves to a directory. The file walker (`_get_all_files_with_extension`) now filters on `is_file()` instead of `exists()`, so a symlink-to-directory (listed as a blob by git but not readable as a file) is skipped instead of raising `FileReadError`.

## [v0.1.18] - 2026-05-28

### Added

- Added: `PREVENT_BARE_TMUX_TARGETS` ratchet rule and `check_bare_tmux_targets` helper that flag `tmux <subcmd> -t '<target>'` invocations whose quoted target doesn't begin with `=` (scans every tracked file type, not just `.py`).
- Added: Promoted `BINARY_FILE_EXCLUSION` to a public `Final` constant in `imbue.imbue_common.ratchet_testing.core` so project ratchets and repo-wide meta-ratchets share one canonical list.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `imbue_common`: extended `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` to align with the wheel-exclude pattern.
