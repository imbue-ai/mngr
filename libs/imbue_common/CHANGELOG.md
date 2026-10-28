# Changelog - imbue_common

A concise, human-friendly summary of changes for the `imbue_common` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.22] - 2026-10-28

### Added

- Added: `smoke` pytest marker registered in the shared markers list, so any project can mark tests that run after a deploy or release.

### Changed

- Changed: The `while True` and inline-import ratchet checks accept `excluded_patterns`.

## [v0.1.21] - 2026-10-22

### Added

- Added: `secret_wrapping` module — pure envelope-encryption helpers (argon2id key derivation, AES-256-GCM DEK wrap/unwrap and secrets encrypt/decrypt) used by the minds workspace-sync feature. New dependencies: `argon2-cffi`, `cryptography`.
- Added: `LogAttachmentGroup.base_dir` — a group with `base_dir` set sweeps that directory instead of the process's log folder, so error reports can attach log files that live outside it (e.g. minds attaching the detached `mngr latchkey forward` daemon's logs). A missing directory matches nothing.
- Added: `get_or_create_anonymous_user_id` (persists a random id to a file, race-safe across processes) plus a required `user_id` argument on `setup_sentry` (attached to every event via `sentry_sdk.set_user`), so Sentry can report the number of distinct installs affected by each issue. No PII is sent; only the opaque id.
- Added: Shared Sentry error-reporting library `imbue.imbue_common.sentry`, packaging the generic pieces previously duplicated in the minds backend: loguru-to-Sentry event/breadcrumb handlers, an unsigned-S3 attachment uploader, a per-exception rate limiter, an oversized-event (HTTP 413) transport, a `before_send` chain (automatic-reporting consent gate and interrupt/clean-shutdown filtering), manual bug-report submission, and a parameterized `setup_sentry`. Callers supply concrete `dsn` / `environment_name` / `s3_attachment_bucket` (plus service name, integrations, and log-attachment groups); no Sentry project/environment/bucket knowledge lives in the library. Adds `sentry-sdk`, `boto3`, and `traceback-with-variables` as `imbue-common` dependencies.
- Added: `setup_sentry` accepts an `ignored_loggers` argument — glob patterns for stdlib logger names whose records must never become Sentry events or breadcrumbs. Sentry's default logging integration patches `logging.Logger.callHandlers` at the class level and captures a logger's ERROR records as events even when the logger has `propagate=False`, so callers that already route a noisy third-party logger's output elsewhere (e.g. into loguru) can now drop the raw records instead of flooding on already-handled noise.
- Added: Shared `PREVENT_ASYNC_AWAIT` ratchet rule (`common_ratchets.py`) and `check_async_await` wrapper (`standard_ratchet_checks.py`) that power a per-project `test_prevent_async_await` ratchet freezing `async def` / `await` usage across the monorepo.
- Added: `LowerCaseStrEnum` in `imbue.imbue_common.enums` — lowercase sibling of `UpperCaseStrEnum`, for enums whose values are an externally visible already-lowercase wire format.
- Added: JSONL log records include a logged exception's formatted traceback (`exception.traceback_text`, capped at 8,000 characters), via `imbue.imbue_common.tracebacks.format_exception_traceback` (moved from mngr).
- Added: `imbue.imbue_common.suspension`, which tells whether the machine was suspended since an earlier moment (the wall clock outran the monotonic clock).
- Added: `owner_exec_client`, a signed client for the owner-exec service: Ed25519 request signing per RFC 9421/9530, with every response verified against the endpoint's pinned SSH host key, failing closed.
- Added: `modal_image_requirements` module (the canonical offline `uv export` command, the `IMAGE_PINNED_PACKAGE_NAMES` registry and export paths) for hash-locked Modal image exports.
- Added: `UnitFloat` primitive, a float validated to the closed interval `[0.0, 1.0]`.
- Added: `parse_iso_timestamp` in `imbue_common.event_envelope`, reading an `IsoTimestamp` into an aware UTC `datetime` (or `None`).
- Added: `testing` extra (`import-linter`, `inline-snapshot`, `pytest`) for downstream suites using the shipped test library; `imbue.imbue_common.pytest_utils` now ships in the wheel.
- Added: Common ratchets `PREVENT_RAW_START_NEW_THREAD`, `PREVENT_RAW_CONCURRENCY_GROUP_EXECUTOR` and `PREVENT_UNPINNED_MODAL_PIP_INSTALL` (`check_raw_start_new_thread`, `check_raw_concurrency_group_executor`, `check_unpinned_modal_pip_install`).
- Added: Optional `excluded_patterns` on `assert_ratchet` and more standard checks (`check_builtin_exception_raises`, `check_getattr`, `check_args_in_docstrings`, `check_returns_in_docstrings`, `check_literal_with_multiple_options`, `check_if_elif_without_else`, `check_async_await`, `check_asyncio_import`), so projects can exclude vendored code.
- Added: Sentry manual bug reports upload the description verbatim to S3 alongside the inline copy, get their own Sentry issue each, and support one-shot attachments by exact path and attachment URIs reserved before upload (archives such as `.zip` are stored as-is).

### Changed

- Changed: `find_bash_scripts_without_strict_mode` (the helper behind the repo-wide bash strict-mode ratchet) now skips `*.sh` files under `.minds/template/`. Those are declarative secret-schema templates sourced by the deploy tooling, not runnable scripts, so `set -euo pipefail` is meaningless for them.
- Changed: `EventEnvelope` ignores unknown fields instead of forbidding them, so older readers tolerate additive fields; subclasses may not re-tighten `extra`.
- Changed: Sentry API: `setup_sentry` gates automatic sends and their attachments on the one `is_error_reporting_enabled` callable (`is_log_inclusion_enabled` removed) and raises the SDK's databag breadth cap so event extras are not trimmed; `submit_manual_bug_report` takes `description` explicitly and drops `include_logs`. The `sentry-sdk` floor is 2.63.0.
- Changed: `check_no_type_errors` runs `ty check --output-format concise` and reports the exact error count, errors before warnings (`TypeCheckOutcome`), instead of dumping raw output.

### Fixed

- Fixed: `PREVENT_TRAILING_COMMENTS` ratchet no longer misfires on `PR #NNNN` references inside comment or docstring prose. The unanchored pattern treated the `#` of a PR number as a trailing comment; a negative lookbehind now exempts a `#` immediately preceded by `PR `, alongside the existing hex-color and `ty: ignore` exemptions.
- Fixed: Inline-function ratchet (`find_inline_functions`) double-counted a function nested two or more levels deep — it walked every `FunctionDef` and descended into all descendants, emitting a doubly-nested function once per ancestor. Nested defs are now keyed by source position and counted once (across the monorepo only `apps/minds` sees a change, from 9 to 7).
- Fixed: `PREVENT_TRAILING_COMMENTS` ratchet no longer misfires on `#{...}` interpolation/format tokens (e.g. tmux format strings like `"#{client_width} #{client_height}"`). The `#` in these tokens is interpolation syntax inside a string, not a comment; real trailing comments, hex colors, and `PR #NNNN` references are unaffected.
- Fixed: Shared ratchet rules no longer flag non-violations: inline imports (docstring prose starting with "import"), trailing comments (bare `#NNNN` references, a hex colour closing a string), `Args:` in docstrings (names like `FooArgs:`), direct subprocess (`subprocess.Popen[bytes]` annotations), async/await (`await-delivery` prose), if/elif without else (an `else:` block starting with `if`), `PREVENT_EXEC` (hyphenated `owner-exec`), and bare tmux targets (tmux ids `$N`/`@N`/`%N`, `$TMUX_PANE`, `*_ID` variables).
- Fixed: Ratchet scans skip binary files (`BINARY_FILE_EXCLUSION`, now including fonts, applied to every scan, plus a NUL-byte sniff) and read undecodable bytes as replacement characters instead of failing with `UnicodeDecodeError`.

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
