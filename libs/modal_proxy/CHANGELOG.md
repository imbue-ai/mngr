# Changelog - modal_proxy

A concise, human-friendly summary of changes for the `modal_proxy` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.2.0] - 2026-10-22

### Added

- Added: `ModalInterface.sandbox_create` accepts an optional `experimental_options` mapping (mirrors `modal.Sandbox.create`), letting callers request Modal's VM runtime via `experimental_options={"vm_runtime": True}`.
- Added: `DirectModalInterface` accepts optional `token_id` / `token_secret`; when set, every SDK call and `modal` CLI subprocess authenticates with them, without mutating the process environment.
- Added: `ModalInterface.is_function_deployed`, which answers immediately whether an app's current deployment publishes a named function.
- Added: `ModalInterface.deploy` takes `extra_env`; `ImageInterface.dockerfile_commands` takes `build_args`.
- Added: `ImageInterface.fetch_build_logs()` returns a failed image build's logs (abstract, so other `ImageInterface` implementations must add it); `modal.exception.ImageBuildError` now translates to the new `ModalProxyImageBuildError` (a `ModalProxyRemoteError`).
- Added: `VolumeInterface.get_object_id()`, to check whether a volume exists without the rate-limited `VolumeListFiles` API.
- Added: Sandbox `poll()` liveness probe reporting a sandbox's authoritative running state.
- Added: Error types `ModalProxyConnectionError` (Modal unreachable), `ModalProxySandboxGoneError` (the sandbox has ended; subclasses `ModalProxyNotFoundError`), and a `ModalProxyTransientError` base for `ModalProxyInternalError`, `ModalProxyRateLimitError`, and the new `ModalProxyServiceError` and `ModalProxyBlockStorageError`.

### Changed

- Changed: Bumped `modal` `1.4.3` -> `1.5.4` and opted into Modal's V2 Sandbox backend (`DirectModalInterface` sets `MODAL_SANDBOX_V2=1` unless already set; `0` opts out). Sandbox `terminate` now blocks until the sandbox is gone.
- Changed: Read-only control-plane calls (`sandbox_list`, `Sandbox.get_tags()` / `poll()` / `tunnels()`, `sandbox_from_id`, `volume_list`, `is_function_deployed`, app lookup) now retry transient failures like volume operations, all under one 60s jittered budget keyed on `ModalProxyTransientError`, each retry logged at warning. "Environment not found" is no longer treated as transient.
- Changed: `DirectModalInterface.deploy` makes a single attempt and leaves retrying to the caller. Retryable failures raise subclasses of the new `ModalProxyDeployRetryableError`: `ModalProxyAppLockedError` (concurrent modification), `ModalProxyImageBuilderLostError` (Modal lost the build worker), and `ModalProxyDeployTimeoutError` (attempt exceeded the now-public `DEPLOY_ATTEMPT_TIMEOUT_SECONDS`, which `deploy_attempt_timeout_seconds` can shorten).

### Fixed

- Fixed: Modal `ServiceError` (gRPC `UNAVAILABLE`, `CANCELLED`, `UNKNOWN`, `DEADLINE_EXCEEDED`) and block-storage 5xx errors are now transient and retried, instead of arriving as the generic, non-retried `ModalProxyError`.
- Fixed: `ExecProcess.wait()` and `ExecOutput.read()` translate Modal exceptions instead of leaking a raw `modal.exception.NotFoundError` when the sandbox ends mid-command.
- Fixed: The `Function fu-<id> not found` form of Modal's concurrent-deploy race is classified as `ModalProxyAppLockedError` instead of failing host creation.

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
