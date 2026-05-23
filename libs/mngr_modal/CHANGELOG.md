# Changelog - mngr_modal

A concise, human-friendly summary of changes for the `mngr_modal` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `ProviderEmptyError` raised by the Modal backend when its per-user environment doesn't exist yet, so `mngr list` silently skips the empty provider instead of aborting.

### Changed

- Changed: CI acceptance wall-clock cut ~62% — `mngr_modal` session-end leak detector reshaped (typed `ModalCleanupOutcome`, `pytest_sessionfinish` hook so it runs after all session-scoped fixture teardowns).
- Changed: Modal provider no longer silently bootstraps an environment from non-create commands — `ProviderUnavailableError` is raised when the per-user Modal environment doesn't exist; only `mngr create` may bootstrap on first use.
- Changed: Modal test fixtures collapse to a single shared environment across an offload-acceptance / offload-release run when `MNGR_TEST_SHARED_MODAL_ENV_NAME` is set, avoiding the per-workspace 1500-env cap during fanout.
- Changed: `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` in addition to `ModalProxyNotFoundError` to handle Modal's async permission propagation (~3–7s) after `modal environment create`.
- Changed: Bumped pinned `modal` dependency from 1.3.1 to 1.4.3 to stay in sync with the rest of the monorepo.

### Fixed

- Fixed: Modal resource leaks in `test_snapshot_and_shutdown.py` — fixture now passes the session-scoped Modal env to deploy/sandbox lookup/volume operations, and cleanup runs `app stop` + `volume delete` in parallel with `check=True`.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr tmr --use-snapshot` modal launches additionally skip the per-agent initial filesystem snapshot.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `mngr_modal` — `ModalMode.TESTING` removed from production paths (tests inject `TestingModalInterface` via `make_testing_provider`); `make_testing_provider` collapsed onto the shared `_construct_modal_provider` factory; `enable_output_capture` is now an abstract method on `ModalInterface`.

### Fixed

- Fixed: `mngr_modal` post-merge CI failure — restored the per-test reset of `ModalProviderBackend._app_registry` (load-bearing for cross-test isolation after the testing-factory reshape).
- Fixed: `mngr_modal` session env registered with the leak-detection registry so silent CLI cleanup failures are caught at session end; CLI cleanup helpers now surface non-zero exits as warnings.
