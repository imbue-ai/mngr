# Changelog - modal_proxy

A concise, human-friendly summary of changes for the `modal_proxy` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.12] - 2026-06-05

### Fixed

- Fixed: Retry `modal deploy` when Modal reports "The selected app is locked - probably due to a concurrent modification". Modal serializes mutations to a single app, so two operations targeting the same app name concurrently (e.g. parallel `mngr create` against the same persistent provider app) would race and one would fail. `DirectModalInterface.deploy` now classifies the transient lock as a new retryable `ModalProxyAppLockedError` and rides through it with exponential backoff; non-lock deploy failures still raise immediately.

## [v0.1.11] - 2026-06-01

## [v0.1.10] - 2026-05-28

### Added

- Added: New `ModalProxyPermissionDeniedError`; `_translate_modal_error` now maps `modal.exception.PermissionDeniedError` to the new typed error (was falling through to the bare `ModalProxyError`).

### Changed

- Changed: Bumped pinned `modal` dependency from 1.3.1 to 1.4.3; `log_utils.py` updated to use Modal 1.4.x's new `RichOutputManager` ABC (the private `OutputManager` API the prior implementation depended on was refactored).

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `modal_proxy`: `ModalInterface.enable_output_capture` is now an abstract method. `DirectModalInterface` hooks into the Modal SDK output system; `TestingModalInterface` returns a `nullcontext`.
