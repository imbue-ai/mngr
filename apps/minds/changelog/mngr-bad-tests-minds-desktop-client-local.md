Audited and hardened the test suite under `desktop_client`, fixing low-quality tests that passed without verifying correctness, broke for unrelated reasons, or implied coverage that did not exist. Highlights:

- Deleted dead reverse-proxy test scaffolding (`_create_multi_backend_http_client`, the `backend_status`/`backend_echo` endpoints, and empty section banners) that read like coverage but exercised nothing; the genuine reverse-proxy request-forwarding path is now explicitly flagged as untested rather than falsely implied.
- Fixed a ConcurrencyGroup leak in `agent_creator_test.py` helpers (they entered groups without exiting them) by routing through the shared `root_concurrency_group` fixture, and removed a `@pytest.mark.flaky` marker whose comment misdescribed the cause.
- Added an autouse fixture that resets the process-global SuperTokens OAuth-flow dict between tests, removing a cross-test isolation hazard.
- Added missing coverage for `recovery_probe` fallback branches (UNKNOWN classifications, curl error vs non-200, dispatch-tier precedence).
- Strengthened many assertions that were too loose (substring/membership/`startswith` where exact equality was warranted, escaping assertions with masking `or` branches, secret-length floors, structural HTML markers instead of bare "id appears somewhere").
- Made timing-sensitive tests deterministic/robust: injected a controllable monotonic clock into `SystemInterfaceHealthTracker` so STUCK-threshold tests no longer rely on sub-50ms wall-clock timing; replaced fixed-`sleep` races and disguised-`Event` sleeps in the destroying and permission-consumer tests with sentinel/condition-based waits and process-group cleanup.

Minor production changes made along the way:

- `backup_export.py` now uses `tempfile.gettempdir()` instead of a hardcoded `/tmp`, matching `webdav.py` and respecting `TMPDIR`.
- `OnboardingApplier` gained an injectable `user_name_resolver` seam so the user-context-scan test is hermetic (no dependency on a real `git` binary).
