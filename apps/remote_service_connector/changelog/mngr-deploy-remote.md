Fixed a pool-host teardown bug where released VPSes were never actually
cancelled (they kept running and billing) with no error surfaced anywhere.

Root cause: the bake wrote the mngr `host_id` into `pool_hosts.vps_instance_id`
instead of the OVH service name, so every connector OVH teardown call
(`vps_urn_for` / `set_delete_at_expiration`) targeted a nonexistent service and
404'd -- and the failure was swallowed into a warning while the release reported
success.

- `POST /hosts/{id}/release` is now **synchronous**: it strips the per-lease OVH
  tags, cancels the VPS, and deletes the row, and returns 200 only when every
  step succeeds. On failure it returns 5xx and leaves the row `removing` so the
  client (or the hourly sweep backstop) retries. `_finish_releasing_pool_host`
  no longer swallows OVH/DB errors -- a release that can't cancel the VPS reports
  failure instead of a false success. Added `PoolHostCleanupError` and mapped it
  plus `OvhApiError`/`OvhHttpError` in `raise_as_http`.
- `cleanup_released_hosts.py` now keys its active-row protection and its
  cleaned-host DB match on `vps_address` (the real OVH service name), not
  `vps_instance_id`. Previously the mismatch meant the runbook protected nothing
  and would have cancelled live leased/available hosts.
- New migration `006_fix_vps_instance_id.sql` backfills existing rows whose
  `vps_instance_id` still holds a `host-...` id.
