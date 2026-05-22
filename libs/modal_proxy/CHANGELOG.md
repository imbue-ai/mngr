# Changelog - modal_proxy

A concise, human-friendly summary of changes for the `modal_proxy` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `ModalProxyPermissionDeniedError` translating `modal.exception.PermissionDeniedError` so callers can retry through Modal's new asynchronous permission-propagation window.

### Changed

- Changed: Bumped pinned `modal` dependency from 1.3.1 to 1.4.3; updated `log_utils.py` to use Modal 1.4.x's new `RichOutputManager` ABC (the private `OutputManager` API was refactored).
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `modal_proxy`: `ModalInterface.enable_output_capture` is now an abstract method. `DirectModalInterface` hooks into the Modal SDK output system; `TestingModalInterface` returns a `nullcontext`.
