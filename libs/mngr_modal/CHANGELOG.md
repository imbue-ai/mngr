# Changelog - mngr_modal

A concise, human-friendly summary of changes for the `mngr_modal` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `ProviderEmptyError` raised by the Modal backend when its per-user environment doesn't exist yet, so `mngr list` can silently skip the empty provider instead of aborting.

### Changed

- Changed: CI acceptance wall-clock cut ~62% — `mngr_modal` session-end leak detector reshaped (typed `ModalCleanupOutcome`, `pytest_sessionfinish` hook so it runs after all session-scoped fixture teardowns).
- Changed: Modal provider no longer auto-creates an environment from non-create commands — only `mngr create` is allowed to bootstrap the per-user Modal environment on first use; `ProviderUnavailableError` is raised otherwise.
- Changed: Modal env creation/deletion can be collapsed across an offload-acceptance / offload-release run to a single shared env via `MNGR_TEST_SHARED_MODAL_ENV_NAME`; modal test fixtures honour the opt-in and skip per-sandbox env creation/leak tracking when set.
- Changed: Bumped pinned `modal` dependency from 1.3.1 to 1.4.3.
- Changed: `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` (in addition to `ModalProxyNotFoundError`) to handle Modal's new asynchronous permission propagation; the test cleanup helper retries SDK deletes through the same window.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

### Fixed

- Fixed: Modal resource leaks in `test_snapshot_and_shutdown.py` — `modal app stop` / `modal volume delete` now run with `check=True` in parallel, and the fixture passes the session-scoped Modal env so the test app + volume land inside the cleanup safety net.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr tmr --use-snapshot` modal launches additionally skip the per-agent initial filesystem snapshot.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `mngr_modal` — `ModalMode.TESTING` removed from production paths (tests inject `TestingModalInterface` via `make_testing_provider`); `make_testing_provider` collapsed onto the shared `_construct_modal_provider` factory; `enable_output_capture` is now an abstract method on `ModalInterface`.

### Fixed

- Fixed: `mngr_modal` post-merge CI failure — restored the per-test reset of `ModalProviderBackend._app_registry` (load-bearing for cross-test isolation after the testing-factory reshape).
- Fixed: `mngr_modal` session env registered with the leak-detection registry so silent CLI cleanup failures are caught at session end; CLI cleanup helpers now surface non-zero exits as warnings.
