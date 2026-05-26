# Changelog - mngr_modal

A concise, human-friendly summary of changes for the `mngr_modal` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: CI acceptance wall-clock cut ~62% — `mngr_modal` session-end leak detector reshaped (typed `ModalCleanupOutcome`, `pytest_sessionfinish` hook so it runs after all session-scoped fixture teardowns).
- Changed: `mngr list` no longer aborts when the Modal per-user environment hasn't been created yet — the backend raises a new `ProviderEmptyError` (distinct from `ProviderUnavailableError`) and the listing pipeline silently skips it. Only `mngr create` is allowed to bootstrap the environment.
- Changed: Modal acceptance / release runs can now opt into a shared env across all sandboxes via `MNGR_TEST_SHARED_MODAL_ENV_NAME` to stay under the 1500-env-per-workspace cap.
- Changed: Bumped pinned `modal` dependency from 1.3.1 to 1.4.3 to stay in sync with the rest of the monorepo.
- Changed: Adopted the new per-project changelog layout.

### Fixed

- Fixed: Both `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` (in addition to `ModalProxyNotFoundError`) to ride out Modal's ~3-7 s async permission-propagation window after `modal environment create`.
- Fixed: Modal resource leaks in `test_snapshot_and_shutdown.py` — teardown's `modal app stop` / `modal volume delete` calls had been silently failing; the fixture now passes `environment_name` to `deploy_function` and runs cleanup with `check=True` in parallel.
- Fixed: `UNABRIDGED_CHANGELOG.md` intro now references the correct entries directory.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr tmr --use-snapshot` modal launches additionally skip the per-agent initial filesystem snapshot.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `mngr_modal` — `ModalMode.TESTING` removed from production paths (tests inject `TestingModalInterface` via `make_testing_provider`); `make_testing_provider` collapsed onto the shared `_construct_modal_provider` factory; `enable_output_capture` is now an abstract method on `ModalInterface`.

### Fixed

- Fixed: `mngr_modal` post-merge CI failure — restored the per-test reset of `ModalProviderBackend._app_registry` (load-bearing for cross-test isolation after the testing-factory reshape).
- Fixed: `mngr_modal` session env registered with the leak-detection registry so silent CLI cleanup failures are caught at session end; CLI cleanup helpers now surface non-zero exits as warnings.
