# Changelog - imbue_common

A concise, human-friendly summary of changes for the `imbue_common` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `PREVENT_BARE_TMUX_TARGETS` ratchet rule and `check_bare_tmux_targets` helper that flag `tmux <subcmd> -t '<target>'` invocations whose quoted target doesn't begin with `=` (scans every tracked file type, not just `.py`).
- Added: Promoted `BINARY_FILE_EXCLUSION` to a public `Final` constant in `imbue.imbue_common.ratchet_testing.core` so project ratchets and repo-wide meta-ratchets share one canonical list.

### Removed

- Removed: `check_no_ruff_errors` helper from `imbue.imbue_common.ratchet_testing.ratchets` — its only callers were the deleted per-project `test_no_ruff_errors` tests, and the repo-wide ruff test runs its own `ruff check` / `ruff format --check` invocations rather than using the helper. `check_no_type_errors` is kept.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `imbue_common`: extended `TEST_FILE_PATTERNS` (used by all standard ratchet checks to skip test files) from `("*_test.py", "test_*.py")` to `("*_test.py", "test_*.py", "conftest.py", "testing.py")` to align with the wheel-exclude pattern.
