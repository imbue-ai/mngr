# Unabridged Changelog - mngr_modal

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_modal/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-06-22

Report an unauthenticated Modal provider consistently with the other cloud providers.

A missing/invalid Modal token now raises the shared `ProviderNotAuthorizedError` from provider construction, and `ModalAuthError` is now a subclass of `ProviderNotAuthorizedError` (preserving its existing message and remediation). As a result, Modal auth failures are categorized the same way as the other cloud providers in `mngr list` -- one consistent error line and the granular provider-inaccessible exit code -- instead of an ad-hoc plugin error.

Fixed host lock reporting for Modal hosts: a host's lock status is now derived from a real flock held-probe rather than the lock file's presence. The lock file now persists after release, so the previous mtime-based check would have reported every previously-locked host as permanently locked.

## 2026-06-19

Fixed a dead relative link in the README so it resolves on the PyPI project page.

Added `test_provider_release_trip1` to the Modal release suite: a single-boot full-lifecycle trip (create, exec, plain stop, `--stop-host` refusal, start, persistence, snapshot, out-of-band kill, gc, backend-clean) built on the shared provider release harness. Modal cannot stop a host's compute, so the trip asserts `mngr stop --stop-host` is refused with `HostShutdownNotSupportedError`; it is not parametrized over isolation (Modal has no isolation modes). Gated on Modal credentials plus the new `MNGR_MODAL_RELEASE_TESTS=1` opt-in.

Also added `test_provider_release_trip3` (snapshot survives destroy): Modal snapshots are portable, so the trip snapshots a host, destroys it, then asserts a fresh `mngr create --snapshot` restores the captured filesystem.

Also added `test_provider_release_trip4` (error classification): a no-boot CLI trip asserting `mngr create` with unresolvable Modal credentials surfaces the right error. This PR also fixes Modal to raise the contract `ProviderUnavailableError` (it previously raised a plain `MngrError`), with curated help pointing at `uvx modal token set`; the trip asserts the contract error class + that help. The `--vps-*` migration-arg scenario is skipped (Modal has its own build-arg parser).

Also added `test_provider_release_trip2` (idle auto-shutdown contract): it caps the sandbox lifetime via `-b --timeout=120` and asserts the sandbox is terminated by Modal's own timeout. Modal has no resumable stopped state, so the trip asserts the termination only and skips the resume.

## 2026-06-18

Agent lifecycle detection now targets the agent's primary tmux window by name (the configurable `tmux.primary_window_name`, default `agent`) instead of the literal `:0` index, so it works regardless of the user's tmux `base-index` setting.

## 2026-06-16

`destroy_host` now raises a `CleanupFailedGroup` carrying the classified cleanup failures (instead of returning them, or swallowing errors as warnings) when a resource is left behind, and returns normally otherwise. A resource that was already gone is treated as benign (no failure); a resource that exists but could not be destroyed is recorded as a `HOST_RESOURCE_REMAINS` failure (or `OTHER` for a bookkeeping/record write failure), so `mngr destroy`/`cleanup` can surface it and exit with an informative, cause-specific code. See `specs/cleanup-error-aggregation.md`.

## 2026-06-12

## AWS provider support: ProviderBackendInterface refactor

`is_for_host_creation` was removed from `ProviderBackendInterface` (Modal-specific flag was being `del`'d in every other backend). Modal now overrides a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method on the interface, where the per-user environment registration moves. `mngr create` invokes this hook before `build_provider_instance`. No behavior change for Modal.

## 2026-06-09

Offline hosts produced by this provider are now readable: the offline-host
construction path (used by both `get_host` for stopped hosts and
`to_offline_host`) returns an `OfflineHostWithVolume` (which implements the new
`HostFileReadInterface`) via the shared `make_readable_offline_host` helper.
This makes a stopped host's files readable through the same interface as an
online host -- used by Claude session preservation when a host is destroyed
while offline (the destroy path obtains the host via `get_host`), and available
to other readers of offline host data. The host's volume is resolved lazily on
first read, so this adds no per-host probe to host discovery. When no volume is
available, reads behave as "nothing there".

The new `get_volume_reference_for_host` is wrapped so missing/expired Modal
credentials surface as the user-friendly `ModalAuthError` (consistent with the
other provider methods) rather than a raw proxy error, including when reached
during offline-host construction.

## Remove Modal async-permission-propagation workaround

Modal has fixed the bug on their side where a just-created environment returned
`modal.exception.PermissionDeniedError` for several seconds (async per-user
permission propagation) before the creating user could operate on it.
Read-after-write is now immediate, so the workaround is no longer needed.

Removed from `mngr_modal`:

- The `ModalProxyPermissionDeniedError` retries in
  `_lookup_persistent_app_with_retry` and `_enter_ephemeral_app_context_with_retry`
  (`imbue.mngr_modal.backend`); both decorators once again retry only on
  `ModalProxyNotFoundError`.
- The `_invoke_modal_sdk_delete_with_retry` test-cleanup helper in `conftest.py`;
  `_classify_modal_sdk_delete` now invokes the SDK delete callable directly again.

## 2026-06-08

Standardized mngr_modal's project conftest on `register_plugin_test_fixtures(globals())`
for HOME isolation, the same single mechanism used by every mngr plugin. The
Modal-specific fixtures (including the credential-loading `setup_test_mngr_env`
override) are unchanged. Internal test-infrastructure change only; no user-facing
behavior change.

Creating a Modal host with an invalid argument (e.g. a non-existent
`--snapshot` image id) now fails with a clean single-line error
(`Error: Failed to create Modal host: '<id>' is not a valid Image ID.`) instead
of dumping a raw Python traceback. `create_host` now wraps
`ModalProxyInvalidError` in a user-facing `MngrError`, mirroring how
`ModalProxyRemoteError` was already handled.

## 2026-06-04

Added an acceptance test (`test_upload_deploy_files_handles_large_set_on_modal`) that uploads a large (600-file) deploy set to a real Modal host and verifies every file lands. This is the regression guard for github issue 1825, where `mngr create` on Modal failed during provisioning (`Error reading SSH protocol banner`) because deploy files were uploaded one SFTP channel at a time; the fix transfers them with a single rsync.

Updated references to the renamed `modal_proxy` test doubles: `TestingModalInterface`
is now `FakeModalInterface` (and the rest of the `Testing*` Modal family is now
`Fake*`). Affects the `make_testing_provider`/`testing_modal` test helpers and
fixtures, which now reference `FakeModalInterface`. No behavior change.

## 2026-06-04

- Three modal acceptance tests (`test_get_host_by_id`, `test_discover_hosts_includes_created_host`, `test_destroy_host_stops_sandbox_and_delete_host_removes_record`) had their `pytest.mark.timeout` bumped from 180s to 300s and a `pytest.mark.flaky` mark added. They all hit `sandbox.tunnels()` while waiting for SSH on a fresh Modal sandbox, which intermittently brushes the 180s ceiling under load -- the bump matches the longer-running peers in the same file (e.g. `test_persistent_host_creates_shutdown_script` already used 300s) and `flaky` lets offload retry on the rare residual timeout.

## 2026-06-02

Preserved connection-error handling in the Modal provider's optimized listing now that
`HostConnectionError` is a `MngrError` subclass. `get_host_and_agent_details` now re-raises
`HostConnectionError` from the inner SSH-collection guard so it still reaches the outer
handler that clears the per-host connection cache and falls back to the default listing,
instead of being swallowed by the inner `except MngrError`.

## 2026-06-01

Fixed flakiness in two `mngr_modal` host-volume acceptance tests by polling `get_volume_for_host` with `wait_for` instead of asserting once, to absorb the brief Modal control-plane lag before a freshly-created volume becomes resolvable by name.

# Offline agent field generators

Updated the provider's `get_host_and_agent_details` override to accept and forward the new `offline_field_generators` parameter to the base implementation, so offline plugin fields (see the mngr changelog entry) are populated when a host falls back to offline data.

## 2026-05-28

# Dropped redundant per-project ty/ruff ratchet tests

Removed this project's `test_no_type_errors` and `test_no_ruff_errors` from its
`test_ratchets.py`. ty resolves the uv workspace root and ruff (run from the repo
root) both scan across projects, so the per-project copies just re-ran the same
checks. The single repo-wide equivalents now live in `test_meta_ratchets.py`
(`test_no_type_errors` and `test_no_ruff_errors`).

No user-facing behavior change.

## 2026-05-27

# Ratchet count tightening

- Tightened the violation counts recorded in `test_ratchets.py` to their current exact values (via `uv run pytest --inline-snapshot=trim`), locking in previously-unrecorded reductions. No source-code or behavior change.

## 2026-05-26

- Pruned non-notable entries (test-only changes, internal refactors, and doc-only tweaks with no user-facing effect) from this project's CHANGELOG.md, per the new notable-only changelog policy.

Adopted the `PREVENT_BARE_TMUX_TARGETS` ratchet rule (added in `imbue_common`) via
`rc.check_bare_tmux_targets(_DIR, snapshot(0))` in this project's `test_ratchets.py`.
This ratchet prevents new occurrences of `tmux <subcmd> -t '<bare-name>'` -- targets
without a leading `=` exact-match prefix, which can silently route commands to a
sibling session whose name shares a prefix with the intended one. No production code
changes in this project; the adopting test starts at a baseline of zero violations.

## 2026-05-21

Fix the intro in `UNABRIDGED_CHANGELOG.md` so it references the correct entries directory. The path was `changelog/<project>/` (which never existed); the actual layout is `<project_dir>/changelog/`.

The acceptance test for `mngr_claude_usage`'s statusline-shim provisioning on a real Modal host (`test_provision_statusline_shim_on_modal_host`) is updated to assert against the new host-stable shim path layout (`<host_dir>/commands/claude_statusline.sh`) and the per-agent sidecar (`<state_dir>/commands/user_statusline_cmd`).

## 2026-05-20

## Modal provider no longer auto-creates an environment from non-create commands

`mngr list`, `mngr gc`, and other read flows no longer silently bootstrap a
Modal environment (the `Created Modal environment: ...` log line) just because
the modal provider is enabled. The Modal provider now disables itself (raises
`ProviderUnavailableError`, which higher-level loaders skip) when its per-user
Modal environment doesn't exist yet. Only `mngr create` is allowed to bootstrap
the environment on first use.

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

- `mngr_modal`: Modal backend now raises a new `ProviderEmptyError`
  (distinct from `ProviderUnavailableError`) when its per-user
  environment doesn't exist yet, so `mngr list` can silently skip the
  empty provider instead of aborting. (Counterpart to the new
  `ProviderEmptyError` handling in the listing pipeline.)

Collapse Modal environments across an offload-acceptance / offload-release
run to a single shared env (opt-in via `MNGR_TEST_SHARED_MODAL_ENV_NAME`).
Each fanned-out sandbox used to mint its own Modal environment and delete
it on teardown -- dozens to hundreds per run, driving the
1500-env-per-workspace cap into transient failures. Inside each sandbox,
the modal test fixtures (`real_modal_provider`, `persistent_modal_provider`,
`initial_snapshot_provider`, plus the session-scoped subprocess-env
fixtures) honor the env var: they thread its name through
`MngrConfig.prefix` + `ModalProviderConfig.user_id` so every test lands
in the shared env, and they skip env creation / deletion / leak-tracking
at the fixture layer (apps and volumes are still created and deleted
per-test as before). Local pytest behavior (no env var set) is unchanged.

Fix Modal resource leaks in `test_snapshot_and_shutdown.py`. The teardown's `modal app stop` and `modal volume delete` calls were both silently failing (`check=False`, captured output discarded); the fixture also wasn't passing an `environment_name` to `deploy_function`, so the test app + volume landed in the default `main` env outside any cleanup safety net. Pass the session-scoped Modal env to deploy, sandbox lookup, volume operations, and cleanup; run app-stop and volume-delete in parallel with `check=True` so any future failure surfaces immediately.

## Retry on async Modal permission propagation

Modal recently migrated their permission system so the per-user permission entry for a just-created environment is propagated asynchronously (typically ~3-7 seconds after `modal environment create` returns success). During that window, operations on the new environment raise `modal.exception.PermissionDeniedError` instead of `NotFoundError`. This was breaking every Modal acceptance test at fixture construction time.

- `imbue.mngr_modal.backend`: both `_enter_ephemeral_app_context_with_retry` and `_lookup_persistent_app_with_retry` now retry on `ModalProxyPermissionDeniedError` in addition to `ModalProxyNotFoundError`, matching the existing 5-attempt exponential backoff (1s→10s) used for env-not-found.
- `libs/mngr_modal/imbue/mngr_modal/conftest.py`: the test cleanup helper `_classify_modal_sdk_delete` now retries Modal SDK deletes through the same propagation window so that fast-running test teardowns don't spuriously leak environments/volumes.

No user-visible behavior change beyond fewer transient Modal failures and a small added startup latency (~3-7s on the very first `mngr create` against a brand-new environment, only on first use per profile).

Applied `@fixture_uses_resources` to `deployed_snapshot_function` in `test_snapshot_and_shutdown.py` to fix `test_snapshot_and_shutdown_missing_host_id` and `test_snapshot_and_shutdown_missing_sandbox_id` failing on the modal resource guard.

Bumped pinned `modal` dependency from 1.3.1 to 1.4.3 to stay in sync with the rest of the monorepo.

## 2026-05-14

CI acceptance test speedup — fix the `mngr_modal` session-end leak detector in `libs/mngr_modal/imbue/mngr_modal/conftest.py` (previously the `modal_session_cleanup` autouse fixture; now a `pytest_sessionfinish` hook so it runs after all session-scoped fixture teardowns -- pytest's autouse session-scoped fixtures tear down before non-autouse session-scoped fixtures regardless of declared dependencies, which made the previous fixture poll a still-registered env and fail before the deregister could run). The detector compared the global `modal environment list --json` against tests' tracked env names, but Modal's listing endpoints are eventually consistent w.r.t. deletion -- after a `modal environment delete X` returns "Environment 'X' not found", the env can still appear in the global list for tens of seconds. With one-test-per-batch the assertion almost never landed in the inconsistency window; with several tests per session it became consistent enough to repeatedly fail teardown on whichever test happened to be last. The fix is twofold: (a) the per-test and session-scope cleanup fixtures deregister tracked resources from `worker_modal_*_names` *only* when the cleanup chain confirmed the resource was deleted or already gone (the synchronous response is authoritative); cleanup failures keep the resource tracked and log a `logger.error` so the session-end leak detector still has a chance to surface a real leak. Cleanup return values are typed via a new `ModalCleanupOutcome` enum (`DELETED | NOT_FOUND | FAILED`). (b) the `pytest_sessionfinish` hook runs after all session-scoped fixture teardowns, so any name still in `worker_modal_*_names` at that point corresponds to a resource whose cleanup either FAILED or was never attempted (test crashed mid-fixture) -- i.e. a real leak rather than a listing-staleness false positive.

## 2026-05-12

TMR: when launching modal agents, override the modal provider config to
skip the per-agent "initial" filesystem snapshot. That snapshot adds 60-90s
per agent and runs once per agent (so 4 agents on a pooled host trigger
four snapshots), even though TMR's pooled hosts are ephemeral and the
snapshotter's host is snapshotted explicitly already.

## 2026-05-08

- mngr_modal: drop `ModalMode.TESTING` from production code paths; tests inject `TestingModalInterface` via `make_testing_provider` instead. Production `mngr_modal.backend` no longer imports `modal_proxy.testing` at module top, so the standard `**/testing.py` wheel-exclude rule applies cleanly to `modal_proxy` (no `only-include` workaround needed) and packaged consumers (e.g. minds.app) no longer crash with `ModuleNotFoundError: No module named 'imbue.modal_proxy.testing'`.
- mngr_modal: `ModalMode` retained with values `DIRECT` (default) and `PROXIED`. `PROXIED` is reserved for routing Modal traffic through the imbue_cloud gateway and currently raises `NotImplementedError` at `build_provider_instance`. The `mode` field on `ModalProviderConfig` is preserved.
- mngr_modal: extract pure `ModalProviderBackend._derive_modal_names(name, config, mngr_ctx)` helper so the environment-name / app-name / host-dir derivation can be unit-tested without instantiating any Modal interface.
- mngr_modal: drop unused `is_testing` parameter from `_get_or_create_app` (only ever non-default in the now-removed `TESTING` dispatch arm; the test-fixture path constructs `ModalProviderApp` directly and never went through this function).

- mngr_modal: extract `ModalProviderBackend._construct_modal_provider(name, config, mngr_ctx, modal_interface)` as the shared factory body. `build_provider_instance` matches the parent-class signature exactly, dispatches on `config.mode` (`DIRECT` selects `DirectModalInterface()`, `PROXIED` raises `NotImplementedError`), then delegates to `_construct_modal_provider`. Tests call `_construct_modal_provider` directly with `TestingModalInterface`. The factory has no per-implementation branches.
- mngr_modal: `make_testing_provider` collapses from a 35-line parallel constructor into a wrapper around `ModalProviderBackend._construct_modal_provider`.
- mngr_modal: delete the dead `mngr_modal/log_utils.py` re-export shim (`b66f3cbd5`'s in-tree migration is complete; nothing imports from it).

- mngr_modal: register the session-scoped Modal env created by `modal_subprocess_env` with the leak-detection registry (`register_modal_test_environment`) so that silent failures in the per-session cleanup helpers (`delete_modal_apps_in_environment` / `delete_modal_volumes_in_environment` / `delete_modal_environment`) are now caught by the autouse `modal_session_cleanup` at session end, rather than leaking the env onto the Modal account.

- mngr_modal: restore the per-test reset of `ModalProviderBackend._app_registry`. The
  autouse `_reset_modal_app_registry` fixture was deleted in #1533. After #1522
  reshaped the test factory to dispatch through `_construct_modal_provider`
  (which short-circuits on the class-level `_app_registry`), the reset became
  load-bearing for cross-test isolation: the second test in a worker would
  reuse the first test's cached app and skip `modal_interface.app_create(...)`,
  leaving `testing_modal._apps` empty and breaking helpers like
  `make_sandbox_with_tags`. Restoring the fixture fixes the post-merge CI
  failures on main.
