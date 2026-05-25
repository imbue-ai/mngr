# Changelog - mngr_modal

A concise, human-friendly summary of changes for the `mngr_modal` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `ProviderEmptyError` (distinct from `ProviderUnavailableError`) — Modal raises it when its per-user environment doesn't exist yet, so `mngr list` silently skips the empty provider instead of aborting.
- Added: Shared Modal env across offload-acceptance / offload-release runs via `MNGR_TEST_SHARED_MODAL_ENV_NAME` opt-in; modal test fixtures thread the env name through `MngrConfig.prefix` + `ModalProviderConfig.user_id` and skip per-sandbox env creation/deletion to stay under Modal's 1500-env-per-workspace cap.

### Changed

- Changed: CI acceptance wall-clock cut ~62% — `mngr_modal` session-end leak detector reshaped (typed `ModalCleanupOutcome`, `pytest_sessionfinish` hook so it runs after all session-scoped fixture teardowns).
- Changed: Modal provider no longer auto-creates an environment from non-create commands; only `mngr create` may bootstrap the environment on first use. Modal backend raises `ProviderUnavailableError` until its env exists.
- Changed: Retry on Modal's async permission propagation — both `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` in addition to `ModalProxyNotFoundError`. Adds ~3-7s on first `mngr create` against a brand-new environment.
- Changed: Bumped pinned `modal` dependency from 1.3.1 to 1.4.3.
- Changed: `mngr_claude_usage` statusline-shim acceptance test on Modal updated to assert against the new host-stable shim path layout.

### Fixed

- Fixed: Modal resource leaks in `test_snapshot_and_shutdown.py` — teardown's `modal app stop` and `modal volume delete` calls were silently failing; the session-scoped Modal env is now passed to deploy, sandbox lookup, volume operations, and cleanup, and app-stop/volume-delete run in parallel with `check=True`.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr tmr --use-snapshot` modal launches additionally skip the per-agent initial filesystem snapshot.

## [v0.2.7] - 2026-05-11

### Changed

- Changed: `mngr_modal` — `ModalMode.TESTING` removed from production paths (tests inject `TestingModalInterface` via `make_testing_provider`); `make_testing_provider` collapsed onto the shared `_construct_modal_provider` factory; `enable_output_capture` is now an abstract method on `ModalInterface`.

### Fixed

- Fixed: `mngr_modal` post-merge CI failure — restored the per-test reset of `ModalProviderBackend._app_registry` (load-bearing for cross-test isolation after the testing-factory reshape).
- Fixed: `mngr_modal` session env registered with the leak-detection registry so silent CLI cleanup failures are caught at session end; CLI cleanup helpers now surface non-zero exits as warnings.
