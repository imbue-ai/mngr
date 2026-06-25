# Changelog - mngr_modal

A concise, human-friendly summary of changes for the `mngr_modal` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Unauthenticated Modal provider now raises the shared `ProviderNotAuthorizedError` from provider construction; `ModalAuthError` is now a subclass of `ProviderNotAuthorizedError`. Modal raises `ProviderUnavailableError` (was a plain `MngrError`) on unresolvable credentials, with curated help pointing at `uvx modal token set`. Modal auth failures are now reported consistently with the other cloud providers in `mngr list` (one consistent error line and the granular provider-inaccessible exit code) instead of an ad-hoc plugin error.
- Changed: Modal agent lifecycle detection now targets the agent's primary tmux window by name (`tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index`.

### Fixed

- Fixed: Host lock reporting for Modal hosts now derives status from a real flock held-probe rather than the lock file's presence (the lock file now persists after release).

## [v0.2.17] - 2026-06-18

## [v0.2.16] - 2026-06-16

### Changed

- Changed: `destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A resource that was already gone is treated as benign; a resource that could not be destroyed is recorded as a `HOST_RESOURCE_REMAINS` failure (or `OTHER` for a bookkeeping write failure), so `mngr destroy`/`cleanup` can surface it and exit with a cause-specific code. See `specs/cleanup-error-aggregation.md`.

## [v0.2.15] - 2026-06-16

## [v0.2.14] - 2026-06-15

## [v0.2.13] - 2026-06-13

### Changed

- Changed: Offline hosts produced by this provider are now readable via the new `HostFileReadInterface` — the offline-host construction path (used by both `get_host` and `to_offline_host`) returns an `OfflineHostWithVolume` via the shared `make_readable_offline_host` helper, with lazy volume resolution (no per-host probe). The new `get_volume_reference_for_host` is wrapped so missing/expired Modal credentials surface as the user-friendly `ModalAuthError` (consistent with the other provider methods), including during offline-host construction.
- Changed: AWS-provider shared-layer refactor — Modal now overrides a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on `ProviderBackendInterface`, where the per-user environment registration moves. `mngr create` invokes this hook before `build_provider_instance`. No behavior change for Modal.

### Removed

- Removed: Modal async-permission-propagation workaround. The `ModalProxyPermissionDeniedError` retries in `_lookup_persistent_app_with_retry` and `_enter_ephemeral_app_context_with_retry` are gone (both decorators once again retry only on `ModalProxyNotFoundError`), and the `_invoke_modal_sdk_delete_with_retry` test-cleanup helper is removed (`_classify_modal_sdk_delete` now invokes the SDK delete callable directly again). Modal has fixed the underlying bug on their side, so read-after-write is immediate and the workaround is no longer needed.

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
