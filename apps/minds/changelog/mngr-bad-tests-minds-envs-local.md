Test-quality improvements under `imbue/minds/envs` (no user-visible behavior change):

- `docker_cleanup_test.py`: the user-id-unresolved skip test now asserts the skip warning is logged (via `capture_loguru`) instead of only "does not raise", so it actually fails if the `cleanup_env_state_container` skip-guard is removed.
- `health_check_test.py`: added coverage for `check_once`'s transport-exception branches (connect error / timeout are transient, other httpx errors are definitive) and for the public `await_apps_healthy` entry point (healthy, definitive-failure, and timeout paths) using `MockTransport` + the `client_factory` seam.
- `generation_test.py` and `providers/cloudflare_tunnels_test.py`: the "not found / 404 is treated as success" tests now assert the underlying delete was actually attempted, not just that no exception was raised.
- `provisioning_test.py`: the deploy re-run test was renamed to describe what it actually guards (one deploy pass per run, not resource-level idempotency), and the two no-inline-rollback failure tests now assert the no-`delete_*`/`wipe_*` invariant directly rather than pinning the exact full call list.
- Consolidated the duplicated `_root_cg` and `_isolated_home` fixtures into a new `imbue/minds/envs/conftest.py`, dropping the redundant `HOME`/`chdir` setup already provided by the autouse isolation fixture.
