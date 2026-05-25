# Changelog - modal_proxy

A concise, human-friendly summary of changes for the `modal_proxy` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `ModalProxyPermissionDeniedError` in `imbue.modal_proxy.errors`; `_translate_modal_error` maps `modal.exception.PermissionDeniedError` to it (was falling through to bare `ModalProxyError`). Surfaces Modal's async permission-propagation window after `modal environment create`.

### Changed

- Changed: Bumped pinned `modal` dependency from 1.3.1 to 1.4.3; `log_utils.py` updated to use Modal 1.4.x's new `RichOutputManager` ABC.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `modal_proxy`: `ModalInterface.enable_output_capture` is now an abstract method. `DirectModalInterface` hooks into the Modal SDK output system; `TestingModalInterface` returns a `nullcontext`.
