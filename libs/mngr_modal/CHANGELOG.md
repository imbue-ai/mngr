# Changelog - mngr_modal

A concise, human-friendly summary of changes for the `mngr_modal` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Security

- Security: The wait for a new sandbox's sshd now verifies the sandbox's host key against the key mngr pinned for it in the provider's `known_hosts` file; a mismatch fails sandbox creation immediately instead of trusting whatever key is presented.

## [v0.3.0] - 2026-10-22

### Added

- Added: `is_vm_runtime_enabled` option under `[providers.modal]` (default false) creates sandboxes on Modal's VM runtime (`experimental_options = {"vm_runtime": True}`) instead of gVisor, giving stronger isolation and broader syscall compatibility (e.g. Docker-in-sandbox state surviving a filesystem snapshot).
- Added: Agent listings populate `AgentDetails.pid` (the agent's main-process PID in the remote host's PID namespace), extracted from the same already-collected tmux/ps probe data.
- Added: A Modal provider instance can carry its own credentials, so instances can target different Modal accounts: `token_id`/`token_secret` (set together; also `MNGR__PROVIDERS__<NAME>__TOKEN_ID`/`__TOKEN_SECRET`), or `modal_profile = "<name>"` naming a `~/.modal.toml` profile, re-read on every provider build (a missing profile raises `ProviderCredentialSourceMissingError`). App contexts are keyed per token.
- Added: `imbue.mngr_modal.config.modal_config_path()` and `read_modal_profiles()` expose the Modal profiles file's location and reader.
- Added: Create steps are reported on mngr's structured progress stream (`--format jsonl`): `modal.build` (with a step per Dockerfile instruction and the image save) and `modal.wait_ssh`.
- Added: `discover_hosts` reports each host's recorded size (`DiscoveredHost.size`: the sandbox's CPUs and memory from its host record); a sandbox without a record carries none.
- Added: The activity watcher, volume sync and shutdown-script logs honor the `host_log_dir` config.

### Changed

- Changed: `discover_hosts_and_agents` now reads a **running** host's agents **live** off the sandbox instead of only from the state volume. Agents created inside the sandbox (e.g. the user's workspace agent) are never written to the outer state volume, so resolving from the volume alone left `mngr destroy` / `mngr start` unable to find a live in-sandbox agent (`Agent not found`), and minds' teardown spun on "Destroying..." forever. Offline hosts still resolve from the volume; if the live read fails the host falls back to the volume. Mirrors the aws/vultr providers.
- Changed: `start_host` auto-restart (no explicit `snapshot_id`) selects the most recent snapshot instead of the initial one. The initial snapshot (taken in `on_agent_created`) still carries the agent and is a valid restore point; a graceful stop/pause leaves a newer snapshot that is preferred. When the host has no snapshots at all it raises `NoSnapshotsModalMngrError` (recreate the workspace rather than restart).
- Changed: Modal provider updated for mngr's new per-provider discovery — implements the bounded `discover_hosts_and_agents_within_timeouts` entry point. Because Modal reads all host and agent state from the state volume in one batched pass, individual host reads are not separately bounded (still bounded by the provider-level discovery error timeout; no host is marked UNKNOWN by this path).
- Changed: Unauthenticated Modal provider now raises the shared `ProviderNotAuthorizedError` from provider construction; `ModalAuthError` is now a subclass of `ProviderNotAuthorizedError`. Modal raises `ProviderUnavailableError` (was a plain `MngrError`) on unresolvable credentials, with curated help pointing at `uvx modal token set`. Modal auth failures are now reported consistently with the other cloud providers in `mngr list` (one consistent error line and the granular provider-inaccessible exit code) instead of an ad-hoc plugin error.
- Changed: Modal agent lifecycle detection now targets the agent's primary tmux window by name (`tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index`.
- Changed: SSH keys are per host, stored per provider instance under `<profile>/providers/modal/<instance>/keys/`: each sandbox gets its own client and sshd host keypair (host key re-injected on every boot, fresh keys for clones, forgotten by `delete_host`), pinned in `keys/known_hosts` by host id; the layout is published as `MODAL_HOST_ACCESS_MANIFEST`. Only `create_host` mints the client key; a host without a per-host one falls back to an operator-installed `keys/modal_ssh_key`, else raises `ModalHostKeyMissingError`. Sandboxes created under the old `providers/modal/` layout are unreachable: destroy and recreate them.
- Changed: Pinned `modal` SDK bumped from 1.4.3 to 1.5.4; sandboxes run on Modal's V2 Sandbox backend (GPU sandboxes stay on V1), and `start_host` confirms a listed sandbox is alive (`poll()`) before treating it as running.
- Changed: The backend registers lazily (`LazyProviderBackend` from the SDK-free `imbue.mngr_modal.plugin` entry point), so `mngr` imports the `modal` SDK only when a Modal provider is used.
- Changed: When Modal streams no output for a failed image build, `mngr create` fetches the failing layer's logs and reports them in the error and the failed host record.
- Changed: A stalled Modal create logs the step it is blocked on: the snapshot-endpoint deploy (after 60s), the sandbox's first command, the SSH tunnel wait, or host-record volume I/O.
- Changed: Agent listings report `initial_branch` as the branch the work_dir is actually on (via `read_checked_out_branch`).
- Changed: Host SSH info in live listings carries `known_hosts_path`, and the displayed `ssh` command adds `-o UserKnownHostsFile=<path> -o StrictHostKeyChecking=yes`.
- Changed: The Modal test-environment reaper moved from mngr core to `imbue.mngr_modal.modal_cli` and `imbue.mngr_modal.cleanup`; `ModalCliOutputError` moved from `imbue.mngr.errors` into the `ModalMngrError` hierarchy.

### Removed

- Removed: The unused `get_ssh_public_key` accessor.

### Fixed

- Fixed: `stop_host` no longer silently swallows a failed `sandbox.terminate()` — it now raises `ModalMngrError`, and `destroy_host` records the failure as a cleanup failure so `mngr destroy` reports failure (and exits non-zero) instead of falsely succeeding while the sandbox keeps running (a billing orphan).
- Fixed: `start_host` now clears `stop_reason` on the persisted host record when it resumes a host from a snapshot. A host that was resumed and then later hard-killed was previously mis-derived as PAUSED/STOPPED instead of CRASHED, so it was never garbage-collected and wrongly showed a Resume control. Mirrors the lima provider's behavior on restart.
- Fixed: Host lock reporting for Modal hosts now derives status from a real flock held-probe rather than the lock file's presence (the lock file now persists after release).
- Fixed: Concurrent `mngr create` on Modal no longer fails on, or queues behind redundant runs of, the app's `snapshot_and_shutdown` endpoint deploy: it deploys only when the app lacks this exact source (a source-digest beacon), adopts a concurrent deploy that won, gets the app atomically before deploying, and retries any Modal-side deploy failure within a budget sized to `modal deploy`'s lock wait; an exhausted budget names the app and the repeating failure.
- Fixed: Transient Modal failures no longer fail unrelated commands: discovery reads retry for up to 60s, and an unreachable or erroring Modal control plane (cancelled, deadline exceeded, unavailable) raises `ProviderUnavailableError`, so multi-provider commands skip Modal and carry on; commands that target Modal still fail, pointing at the network and https://status.modal.com.
- Fixed: Host bring-up survives Modal killing or ending a new sandbox: the sandbox is probed and replaced (up to three tries) instead of failing `mngr create`/`mngr start`, a step that fails because the sandbox died raises `ModalSandboxDiedMngrError` instead of blaming package installation, and sshd readiness waits until sessions can open ("No existing session").
- Fixed: `mngr create`, `mngr snapshot create` and `mngr stop`'s pre-termination snapshot no longer fail on SSH banner/readiness errors right after a filesystem snapshot: the snapshot record is written over the already-open connection, which also drops a second SSH connection (~6s) per create.
- Fixed: Dockerfiles that use an `ARG` (including mngr's default `resources/Dockerfile`) build correctly on Modal: instructions are batched per cached layer with `ARG`s kept in scope, and `-b docker-build-arg=KEY=VALUE` is passed as a real build arg (values containing `"`, `$` or `\` no longer corrupt the Dockerfile). Changing a build arg now rebuilds every layer, and the first build after upgrading misses the existing layer cache.
- Fixed: Hosts created without an explicit image or Dockerfile no longer become unreachable over SSH (`Error reading SSH protocol banner`): the fallback image is `python:3.12-slim-trixie` instead of `debian_slim()` (bookworm), so workspace setup no longer upgrades sshd across Debian releases under the running daemon.
- Fixed: The `snapshot_and_shutdown` endpoint waits for the sandbox to actually stop (returning its exit code) instead of reporting success while it keeps running and billing.
- Fixed: `get_volume_for_host` no longer reports an existing volume as missing when Modal's `VolumeListFiles` rate limit is hit; existence is checked by resolving the volume id.
- Fixed: `list_snapshots` reads the host record uncached, so it no longer returns an empty list right after a snapshot.
- Fixed: Every sandbox boot restores `/tmp`'s sticky bit (`1777`), which Modal drops during image builds and snapshots, breaking tools such as codex's app-server.
- Fixed: Sandbox sshd uses the shared `SSHD_START_OPTIONS` (`ClientAliveInterval 30`, `ClientAliveCountMax 4`), so a silently dead client is reaped in about two minutes instead of ~2h and no longer holds host locks for hours.
- Fixed: Worker threads and parallel fan-outs (`start_mngr_thread`, `mngr_executor`) destroy their gevent Hub when done instead of leaking it and its pipe pair for the life of the process.

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
