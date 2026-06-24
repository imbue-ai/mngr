# Changelog - modal_proxy

A concise, human-friendly summary of changes for the `modal_proxy` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.18] - 2026-06-18

## [v0.1.17] - 2026-06-16

## [v0.1.16] - 2026-06-16

### Fixed

- Fixed: Post-deploy `DirectFunction.get_web_url` lookup now retries with backoff on `NotFoundError`, riding through the brief deploy-then-lookup propagation delay instead of failing immediately.

## [v0.1.15] - 2026-06-15

## [v0.1.14] - 2026-06-13

### Removed

- Removed: `ModalProxyPermissionDeniedError` error class (`imbue.modal_proxy.errors`) and the `_translate_modal_error` branch that mapped `modal.exception.PermissionDeniedError` to it (`imbue.modal_proxy.direct`); permission-denied errors once again fall through to the base `ModalProxyError`. Modal has fixed the underlying async-permission-propagation bug on their side (read-after-write is now immediate), so the workaround is no longer needed.

## [v0.1.13] - 2026-06-08

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
