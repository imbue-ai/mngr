Removed all OVH logic from the remote connector service. Pool hosts are now exclusively bare-metal slices, so releasing a host destroys its slice's lima VM and the connector makes no OVH API calls.

- The `/hosts/{id}/release` route is slice-only (no OVH tag-strip/cancel); a failed teardown returns 5xx and leaves the row `removing`.

- Removed the OVH cleanup sweep from the hourly `cleanup_removing_pool_hosts` cron; the cron now only runs the alert-only slice-box reconcile.

- Dropped the `ovh` Python dependency and the `ovh-<env>` Modal secret from the deployment.

- Added migration `012_drop_pool_host_backend_kind.sql`: deletes any residual `ovh_vps` rows and drops the `pool_hosts.backend_kind` column.

Known follow-up: a slice row left in `removing` by a crashed inline release is no longer auto-swept (only alert-only reconcile remains).
