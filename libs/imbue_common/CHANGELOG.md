# Changelog - imbue_common

A concise, human-friendly summary of changes for the `imbue_common` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Shared Sentry error-reporting library `imbue.imbue_common.sentry`, packaging the generic pieces previously duplicated in the minds backend: loguru-to-Sentry event/breadcrumb handlers, an unsigned-S3 attachment uploader, a per-exception rate limiter, an oversized-event (HTTP 413) transport, a `before_send` chain (automatic-reporting consent gate and interrupt/clean-shutdown filtering), manual bug-report submission, and a parameterized `setup_sentry`. Callers supply concrete `dsn` / `environment_name` / `s3_attachment_bucket` (plus service name, integrations, and log-attachment groups); no Sentry project/environment/bucket knowledge lives in the library. Adds `sentry-sdk`, `boto3`, and `traceback-with-variables` as `imbue-common` dependencies.
- Added: `setup_sentry` accepts an `ignored_loggers` argument — glob patterns for stdlib logger names whose records must never become Sentry events or breadcrumbs. Sentry's default logging integration patches `logging.Logger.callHandlers` at the class level and captures a logger's ERROR records as events even when the logger has `propagate=False`, so callers that already route a noisy third-party logger's output elsewhere (e.g. into loguru) can now drop the raw records instead of flooding on already-handled noise.
- Added: Shared `PREVENT_ASYNC_AWAIT` ratchet rule (`common_ratchets.py`) and `check_async_await` wrapper (`standard_ratchet_checks.py`) that power a per-project `test_prevent_async_await` ratchet freezing `async def` / `await` usage across the monorepo.
- Added: `LowerCaseStrEnum` in `imbue.imbue_common.enums` — lowercase sibling of `UpperCaseStrEnum`, for enums whose values are an externally visible already-lowercase wire format.

### Changed

- Changed: `find_bash_scripts_without_strict_mode` (the helper behind the repo-wide bash strict-mode ratchet) now skips `*.sh` files under `.minds/template/`. Those are declarative secret-schema templates sourced by the deploy tooling, not runnable scripts, so `set -euo pipefail` is meaningless for them.

### Fixed

- Fixed: `PREVENT_TRAILING_COMMENTS` ratchet no longer misfires on `PR #NNNN` references inside comment or docstring prose. The unanchored pattern treated the `#` of a PR number as a trailing comment; a negative lookbehind now exempts a `#` immediately preceded by `PR `, alongside the existing hex-color and `ty: ignore` exemptions.
- Fixed: Inline-function ratchet (`find_inline_functions`) double-counted a function nested two or more levels deep — it walked every `FunctionDef` and descended into all descendants, emitting a doubly-nested function once per ancestor. Nested defs are now keyed by source position and counted once (across the monorepo only `apps/minds` sees a change, from 9 to 7).

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
