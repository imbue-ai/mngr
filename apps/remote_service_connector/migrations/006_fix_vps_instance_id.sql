-- Migration: backfill pool_hosts.vps_instance_id with the OVH service name.
--
-- The bake (`mngr imbue_cloud admin pool create`) historically wrote the mngr
-- `host_id` (a `host-...` id) into `vps_instance_id` instead of the OVH service
-- name (the `vps-xxxx.vps.ovh.us` hostname, which equals `vps_address` for these
-- OVH-backed pool hosts). Every connector-side OVH teardown keys on this column
-- (`vps_urn_for` / `set_delete_at_expiration` in `clean_up_pool_host_in_ovh`, the
-- release route, and the hourly cleanup sweep), so a `host-...` value made those
-- calls target a nonexistent OVH service -> the cancel silently 404'd and the VPS
-- was never cancelled (kept billing). The bake now writes `vps_address`; this
-- backfills the rows written by the buggy version.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/006_fix_vps_instance_id.sql
--
-- Idempotent: once every row's vps_instance_id is a service name, the WHERE
-- clause matches nothing on a rerun.

BEGIN;

-- Only touch rows whose vps_instance_id still holds a host id (the bug
-- signature). Correct rows already carry the `vps-...` service name and are
-- left untouched. vps_address is the OVH service name for these rows.
UPDATE pool_hosts
SET vps_instance_id = vps_address
WHERE vps_instance_id LIKE 'host-%';

COMMIT;
