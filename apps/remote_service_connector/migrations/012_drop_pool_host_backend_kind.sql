-- Migration: drop pool_hosts.backend_kind (slices are the only backend now).
--
-- The legacy OVH-VPS pool backend (backend_kind = 'ovh_vps') has been removed:
-- every pool host is now a "slice" (a lima VM on one of our bare_metal_servers),
-- so the backend_kind discriminator no longer carries information. Any residual
-- 'ovh_vps' rows are deleted (they reference VPSes the connector can no longer
-- tear down), then the column is dropped.
--
-- Apply with:
--     psql "$NEON_DB_DIRECT" -f apps/remote_service_connector/migrations/012_drop_pool_host_backend_kind.sql
--
-- No IF NOT EXISTS guard: schema_migrations is the source of truth for which
-- migrations have run.

BEGIN;

DELETE FROM pool_hosts WHERE backend_kind = 'ovh_vps';

ALTER TABLE pool_hosts DROP COLUMN backend_kind;

COMMIT;
