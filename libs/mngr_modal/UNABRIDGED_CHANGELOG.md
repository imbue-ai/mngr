# Unabridged Changelog - mngr_modal

Full, unedited changelog entries consolidated nightly from individual files in `libs/mngr_modal/changelog/`.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

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
