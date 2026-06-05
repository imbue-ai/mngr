Test-quality cleanup for the OVH provider (no user-facing behavior change beyond one bug fix):

- `write_pending_order_marker` now wraps its directory `mkdir` in the same `try/except OSError -> MngrError` as the file write, so a directory-creation failure surfaces as a loud `MngrError` (matching the documented "losing a marker must fail loudly" contract) instead of escaping as a raw `OSError`.
- Replaced several low-signal tests with behavioral ones: `attach_tags` now asserts the per-pair POST path + body (not just "two POSTs"); the OVH rebuild test asserts the rebuild was actually POSTed with the right image/key and that the task was polled to `done`; `wait_for_ssh_after_rebuild` asserts it connects to the right `(host, port)` exactly once; the pending-order marker test pins the exact on-disk JSON schema; the marker OSError test asserts specifically `MngrError`.
- Removed tautological tests (a `hasattr` import check; a release smoke test that only re-asserted a fresh client's in-memory SSH cache is empty).
- Release-test agent names now use `uuid4().hex` instead of `time.time() % 100000`, so overlapping runs cannot collide on the name of a real billed VPS.
- Flattened test grouping classes in `client_test.py` and `test_release_ovh.py` into module-level `test_*` functions per the style guide.
