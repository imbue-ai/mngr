# Changelog - mngr_modal

A concise, human-friendly summary of changes for the `mngr_modal` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.2.12] - 2026-06-08

### Fixed

- Fixed: Creating a Modal host with an invalid argument (e.g. a non-existent `--snapshot` image id) now fails with a clean single-line `Error: Failed to create Modal host: '<id>' is not a valid Image ID.` instead of dumping a raw Python traceback — `create_host` now wraps `ModalProxyInvalidError` in a user-facing `MngrError`, mirroring how `ModalProxyRemoteError` was already handled.

## [v0.2.11] - 2026-06-05

## [v0.2.10] - 2026-06-01

### Changed

- Changed: Provider's `get_host_and_agent_details` override now accepts and forwards the new `offline_field_generators` parameter to the base implementation, so offline plugin fields are populated when a host falls back to offline data.

## [v0.2.9] - 2026-05-28

### Changed

- Changed: `mngr list` no longer aborts when the Modal per-user environment hasn't been created yet — the backend raises a new `ProviderEmptyError` (distinct from `ProviderUnavailableError`) and the listing pipeline silently skips it. Only `mngr create` is allowed to bootstrap the environment.

### Fixed

- Fixed: Both `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` (in addition to `ModalProxyNotFoundError`) to ride out Modal's ~3-7 s async permission-propagation window after `modal environment create`.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr tmr --use-snapshot` modal launches additionally skip the per-agent initial filesystem snapshot.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `mngr_modal` — `ModalMode.TESTING` removed from production paths (tests inject `TestingModalInterface` via `make_testing_provider`); `make_testing_provider` collapsed onto the shared `_construct_modal_provider` factory; `enable_output_capture` is now an abstract method on `ModalInterface`.
